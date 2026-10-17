#ifndef SERVER_TOOLS_HPP
#define SERVER_TOOLS_HPP

#include <cstdint>
#include <algorithm>
#include <charconv>

#include <fstream>
#include <iostream>
#include <string>
#include <string_view>

#include <openssl/ssl.h>
#include <sys/socket.h>
#include <unistd.h>

#include <curl/curl.h>
#include "http1_header.hpp"

/*
 * Read the complete header, retaining any body bytes already received.
 */
inline bool server_read_header(SSL *conn_ssl, std::string &raw) {
  char buffer[1000];
  std::size_t end;
  while ((end = raw.find("\r\n\r\n")) == std::string::npos && raw.size() <= 8192) {
    int size = SSL_read(conn_ssl, buffer, sizeof(buffer));
    if (size <= 0) return false;
    raw.append(buffer, size);
  }
  return end != std::string::npos && end <= 8192;
}

/*
 * Close connection and free relevant objects.
 */
inline void server_close_conn(SSL *conn_ssl, std::int32_t conn_sfd) {
  SSL_shutdown(conn_ssl);
  SSL_free(conn_ssl);
  shutdown(conn_sfd, SHUT_RDWR);
  close(conn_sfd);
}

/*
 * Content type from the path's extension, for the handful the site serves
 */
inline const char *mime(std::string &path) {
  if (path.ends_with(".html"))
    return "text/html";
  if (path.ends_with(".css"))
    return "text/css";
  if (path.ends_with(".js") || path.ends_with(".ts"))
    return "text/javascript";
  if (path.ends_with(".json"))
    return "application/json";
  if (path.ends_with(".svg"))
    return "image/svg+xml";
  if (path.ends_with(".png"))
    return "image/png";
  if (path.ends_with(".jpg") || path.ends_with(".jpeg"))
    return "image/jpeg";
  if (path.ends_with(".ttf"))
    return "font/ttf";
  return "application/octet-stream";
}

/*
 * Read characters from file into body
 */
inline bool read_file(std::string &file, std::string &body) {
  std::ifstream html_stream(file, std::ios::binary);
  if (!html_stream) {
    std::cerr << "Couldn't open " << file << "\n";
    return 1;
  }
  html_stream.seekg(0, std::ios::end);
  std::size_t html_size = html_stream.tellg(); // Get size of html file
  body.resize(html_size, '\0');
  html_stream.seekg(0, std::ios::beg);      // Rewind before reading
  html_stream.read(body.data(), html_size); // body
  return 0;
}

/*
 * Construct and send response buffer
 */
inline bool server_send_response(std::string_view status,
                                 std::string_view content_type,
                                 std::string_view body, SSL *conn_ssl,
                                 std::string_view cache = "no-cache") {
  // Construct response
  std::string response = "HTTP/1.1 " + std::string(status) +
                         "\r\n"
                         "Content-Type: " +
                         std::string(content_type) +
                         "; charset=utf-8\r\n"
                         "Content-Length: " +
                         std::to_string(body.size()) +
                         "\r\n"
                         "Cache-Control: " +
                         std::string(cache) +
                         "\r\n"
                         "Connection: close\r\n"
                         "\r\n"; // header
  response.reserve(response.size() + body.size());
  response += body; // Append body to header

  // Send response buffer
  if (SSL_write(conn_ssl, response.c_str(),
                static_cast<std::int32_t>(response.size())) <= 0) {
    std::cout << "Send failed\n";
    return 1;
  }

  return 0;
}

/*
 * Serves the file in the endpoint relative to the path in rel
 */
inline bool server_send_file(std::string_view rel, std::string_view endpoint,
                             SSL *conn_ssl) {

  // Check if the endpoint is valid
  if (endpoint.find("..") != std::string_view::npos ||
      endpoint.find('\0') != std::string_view::npos) {
    std::cerr << "Rejected endpoint: " << endpoint << "\n";
    return server_send_response("400 Bad Request", "text/plain", "", conn_ssl);
  }

  // Construct absolute file path
  std::string path(rel);
  path.append(endpoint);
  if (path.find(".") == std::string::npos) { // if no file extension
    path.append("/index.html");              // assume nested index.html
  }

  // Read and send path
  std::string body;
  if (read_file(path, body)) {     // If reading file fails
    if (path.ends_with(".html")) { // and requested an html
      std::string path404(rel);    // read and send 404.html
      path404.append("errs/404.html");
      read_file(path404, body);
      return server_send_response("404 Not Found", "text/html", body, conn_ssl);
    } else { // and request was some non-html content
      return server_send_response("400 Bad Request", "text/plan", "", conn_ssl);
    }
  }

  // Images rarely change, so browsers may keep them for a day; pages,
  // styles and scripts are revalidated so edits show up right away
  std::string_view type = mime(path);
  bool image = type.starts_with("image/");
  return server_send_response("200 OK", type, body, conn_ssl,
                              image ? "public, max-age=86400" : "no-cache");
}

/*
 * Forwards an HTTP request to a local server, typically hosting some app
 */
inline void server_forward(const std::string &url, SSL *conn_ssl,
                           HTTP1Header &request, const std::string &raw) {
  std::string request_body;
  if (request.keyword == "POST") {
    std::string length(request.field(raw, "Content-Length"));
    std::size_t size = 0;
    auto parsed = std::from_chars(length.data(), length.data() + length.size(), size);
    if (length.empty() || parsed.ec != std::errc() ||
        parsed.ptr != length.data() + length.size() || size > 65536 ||
        !request.field(raw, "Transfer-Encoding").empty()) {
      server_send_response("400 Bad Request", "text/plain", "Invalid body length", conn_ssl, "no-store");
      return;
    }
    request_body = raw.substr(raw.find("\r\n\r\n") + 4, size);
    char buffer[1000];
    while (request_body.size() < size) {
      int received = SSL_read(conn_ssl, buffer, std::min(sizeof(buffer), size - request_body.size()));
      if (received <= 0) break;
      request_body.append(buffer, received);
    }
    if (request_body.size() != size) {
      server_send_response("400 Bad Request", "text/plain", "Incomplete body", conn_ssl, "no-store");
      return;
    }
  }
  CURL *curl = curl_easy_init();
  std::string body;
  if (curl) {
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 120L);
    curl_slist *headers = nullptr;
    for (const char *name : {"Authorization", "Origin"}) {
      std::string_view value = request.field(raw, name);
      if (!value.empty()) {
        headers = curl_slist_append(headers, (std::string(name) + ": " + std::string(value)).c_str());
      }
    }
    if (request.keyword == "POST") {
      headers = curl_slist_append(headers, ("Content-Type: " + std::string(request.field(raw, "Content-Type"))).c_str());
      curl_easy_setopt(curl, CURLOPT_POSTFIELDS, request_body.c_str());
      curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE_LARGE, static_cast<curl_off_t>(request_body.size()));
    }
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(
        curl, CURLOPT_WRITEFUNCTION,
        +[](char *data, std::size_t size, std::size_t count,
            void *out) -> std::size_t {
          static_cast<std::string *>(out)->append(data, size * count);
          return size * count;
        });
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &body);
    CURLcode result = curl_easy_perform(curl);
    long status = 502;
    char *type = nullptr;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
    curl_easy_getinfo(curl, CURLINFO_CONTENT_TYPE, &type);
    server_send_response(
        result == CURLE_OK ? std::to_string(status) + " " : "502 Bad Gateway",
        type ? type : "text/plain",
        result == CURLE_OK ? body : "Upstream unavailable", conn_ssl, "no-store");
    curl_easy_cleanup(curl);
    curl_slist_free_all(headers);
  } else {
    server_send_response("502 Bad Gateway", "text/plain",
                         "Upstream unavailable", conn_ssl, "no-store");
  }
}

#endif
