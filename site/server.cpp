#include <cstddef> // std::size_t
#include <cstdint> // types (uint32_t/int32_t, uint16_t/int16_t, ...);
#include <cstdlib> // strtol
#include <cstring> // memset

#include <iostream>
#include <string>      // Owning strings
#include <string_view> // Non owning string

#include <arpa/inet.h>   // Getting IP from byte order uint32_t
#include <netinet/in.h>  // Main internet addr struct (sockaddr_in)
#include <openssl/ssl.h> // TLS library for HTTPS (requires -lssl & -lcrypto)
#include <sys/socket.h>  // C sockets
#include <unistd.h>      // Close C socket file descriptors (close())

#include "server_tools/http1_header.hpp" // HTTP1.1 header object
#include "server_tools/http2_header.hpp" // TBA...
#include "server_tools/server_tools.hpp" // Internal functions

#ifndef SOURCE_PATH // -DSOURCE_PATH='"/absolute/path/to/src/"' to compiler ( w/ trailing slash)
#define SOURCE_PATH ""
#endif
#define SITE_DIR "site/" // with trailing slash
#define API_PREFIX "api/auth/" // requests to /api/auth/... get forwarded
#define API_UPSTREAM "http://127.0.0.1:1026/auth/" // local auth service (w/ trailing slash)
#define NODES_PREFIX "api/nodes/" // requests to /api/nodes/... get forwarded
#define NODES_UPSTREAM "http://127.0.0.1:1028/nodes/" // local node config API (w/ trailing slash)
#define OPENAI_PREFIX "api/v1/" // requests to /api/v1/... get forwarded
#define OPENAI_UPSTREAM "http://127.0.0.1:1030/v1/" // local OpenAI-compatible API (w/ trailing slash)

int main() {
  // TLS Encryption cert (w/ its chain, ex: Let's Encrypt's fullchain.pem) and key
  SSL_CTX *ssl_ctx = SSL_CTX_new(TLS_server_method());
  if (!ssl_ctx) {
    std::cout << "Error when creating SSL server context\n";
    return 1;
  }
  if (SSL_CTX_use_certificate_chain_file(ssl_ctx, SOURCE_PATH "creds/localhost+2.pem") <= 0 ||
      SSL_CTX_use_PrivateKey_file(ssl_ctx,
                                  SOURCE_PATH "creds/localhost+2-key.pem",
                                  SSL_FILETYPE_PEM) <= 0 ||
      SSL_CTX_check_private_key(ssl_ctx) != 1) {
    std::cout << "Error when assigning certs and keys to SSL context\n";
    return 1;
  }

  // Create (soon-to-be) listening socket
  std::uint32_t addr_fam = AF_INET; // socket address family (ex: ipv6, BT)

  std::int32_t listen_sfd = socket(addr_fam, SOCK_STREAM, 0);
  if (listen_sfd == -1) {
    std::cout << "Creation of the socket failed\n";
    return 1;
  }

  // Bind to local internet address + port (1025)
  std::uint16_t port = 1025;
  struct sockaddr_in server_addr;
  std::memset(&server_addr, 0, sizeof(server_addr));
  server_addr.sin_family = addr_fam;
  server_addr.sin_port = htons(port);
  server_addr.sin_addr.s_addr = htonl(INADDR_ANY);
  std::int32_t bind_err = bind(listen_sfd, (sockaddr *)&server_addr,
                               sizeof(server_addr)); // Bind
  if (bind_err < 0) {
    std::cout << "Bind failed with error: " << errno
              << "\n"; // ex: EADDRINUSE = 98
    return 1;
  }

  // Listen for connections on socket
  if (listen(listen_sfd, 10) == -1) {
    std::cout << "Bind failed with error: " << errno << "\n";
  }

  std::cout << "server running at localhost:" << port << "\n";

  // Server loop
  while (true) {

    // Initialize client variables
    struct sockaddr_in client_addr;
    socklen_t client_addr_len = sizeof(client_addr);
    char client_ip[INET_ADDRSTRLEN];

    // Get socket holding remote client as peer
    std::int32_t conn_sfd =
        accept(listen_sfd, (sockaddr *)&client_addr, &client_addr_len);
    if (conn_sfd == -1) {
      std::cout << "Accept failed with " << errno << "\n";
      continue;
    }
    inet_ntop(addr_fam, &client_addr.sin_addr.s_addr, client_ip,
              sizeof(client_ip)); // Decode IP
    std::cout << "\n-------------\nConn. with client: " << client_ip << "...\n";

    // TLS handshake with key
    SSL *conn_ssl = SSL_new(ssl_ctx);
    SSL_set_fd(conn_ssl, conn_sfd);

    // Wait for TLS/SSL client to send handshake bytes
    if (SSL_accept(conn_ssl) <= 0) {
      std::cout << "Secure connection failed\n";
      server_close_conn(conn_ssl, conn_sfd);
      continue;
    }

    // Read decrypted client contents
    std::string raw;
    if (!server_read_header(conn_ssl, raw)) {
      std::cout << "Read from client failed\n";
      server_close_conn(conn_ssl, conn_sfd);
      continue;
    }

    // Parse HTTPS request header
    HTTP1Header request_header(raw.c_str(), raw.find("\r\n\r\n") + 4);
    if (request_header.fail) {
      server_send_file(SOURCE_PATH SITE_DIR, "404.html", conn_ssl);
      server_close_conn(conn_ssl, conn_sfd);
      continue;
    }
    std::size_t query =
        request_header.endpoint.find('?'); // ...le.com/about?key=value
                                           //                ^
    std::string_view path = request_header.endpoint.substr(0, query);
    // Login query strings contain pending registration codes; keep them out of logs.
    std::cout << "Request: " << request_header.keyword << " " << path << "\n";

    /****************************Request Handling****************************/
    if (path.starts_with(API_PREFIX) &&
        (request_header.keyword == "GET" || request_header.keyword == "POST")) { // API
      // Forward to the local authentication service
      std::string url = API_UPSTREAM + std::string(request_header.endpoint.substr(
                                           sizeof(API_PREFIX) - 1));
      server_forward(url, conn_ssl, request_header, raw);
    } else if (path.starts_with(NODES_PREFIX) &&
               (request_header.keyword == "GET" || request_header.keyword == "POST")) { // Node config API
      // Forward to the local node config API (sinference/backend/nodes)
      std::string url = NODES_UPSTREAM + std::string(request_header.endpoint.substr(
                                             sizeof(NODES_PREFIX) - 1));
      server_forward(url, conn_ssl, request_header, raw);
    } else if (path.starts_with(OPENAI_PREFIX) &&
               (request_header.keyword == "GET" || request_header.keyword == "POST")) { // OpenAI-compatible API
      // Forward to the orchestrator (sinference/backend/orchestrator)
      std::string url = OPENAI_UPSTREAM + std::string(request_header.endpoint.substr(
                                              sizeof(OPENAI_PREFIX) - 1));
      server_forward(url, conn_ssl, request_header, raw);
    } else if (request_header.keyword == "GET") { // GET a file in site dir
      server_send_file(SOURCE_PATH SITE_DIR, path, conn_ssl);
    } else { // Malformed request
      server_send_response("405 Method Not Allowed", "text/plain", "",
                           conn_ssl);
    }

    // close
    server_close_conn(conn_ssl, conn_sfd);
  }

  // Free relevent objects
  SSL_CTX_free(ssl_ctx);

  // Shutdown and close listening socket
  shutdown(listen_sfd, SHUT_RDWR);
  close(listen_sfd);

  return 0;
}
