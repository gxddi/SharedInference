#ifndef HTTP1_HEADER_HPP
#define HTTP1_HEADER_HPP

#include <cctype>  // tollower
#include <cstddef> // std::size_t

#include <iostream>

#include <string_view> // Non owning string

struct HTTP1Header {
  std::string_view keyword;    // ex: GET, POST, ...
  std::string_view endpoint;   // ex: /index.html
  std::string_view version;    // ex: HTTP/1.1
  std::string_view connection; // ex: keep-alive
  std::string_view accept;     // ex: text/html
  bool fail = true;            // false for success

  // Field names are case-insensitive ASCII tokens (RFC 9110)
  static bool iequals(std::string_view a, std::string_view b) {
    if (a.size() != b.size())
      return false;
    for (std::size_t i = 0; i < a.size(); ++i) {
      // tolower is UB on negative values, so widen through unsigned char
      unsigned char ca = static_cast<unsigned char>(a[i]);
      unsigned char cb = static_cast<unsigned char>(b[i]);
      if (std::tolower(ca) != std::tolower(cb))
        return false;
    }
    return true;
  }

  // Value of a header field, minus surrounding whitespace. Empty when absent.
  inline std::string_view field(std::string_view raw, std::string_view name) {
    std::size_t pos = raw.find("\r\n"); // Skip the request line
    if (pos == std::string_view::npos)
      return {};
    pos += 2;

    while (pos < raw.size()) {
      std::size_t eol = raw.find("\r\n", pos);
      std::size_t stop = (eol == std::string_view::npos) ? raw.size() : eol;
      std::string_view line = raw.substr(pos, stop - pos);
      if (line.empty()) // Blank line ends the header section
        break;

      std::size_t colon = line.find(':');
      if (colon != std::string_view::npos &&
          iequals(line.substr(0, colon), name)) {
        std::string_view value = line.substr(colon + 1);
        std::size_t first =
            value.find_first_not_of(" \t"); // Optional whitespace
        if (first == std::string_view::npos)
          return {};
        std::size_t last = value.find_last_not_of(" \t");
        return value.substr(first, last - first + 1);
      }

      if (eol == std::string_view::npos)
        break;
      pos = eol + 2;
    }
    return {};
  }

  /*
   * Constructor from raw HTTP text, parsing n char's (instead of std::string to
   * avoid pointless deep copy)
   */
  inline HTTP1Header(const char *raw, std::size_t n) {

    std::string_view raw_view(raw, n);

    // Request line
    std::size_t line_end = raw_view.find("\r\n");
    if (!line_end) {
      std::cerr << "Invalid HTTP header. No request line.\n";
      return;
    }
    std::string_view request_line = raw_view.substr(0, line_end);

    // Keyword                                      GET /index.html
    std::size_t kw_end = request_line.find(' '); //    ^
    if (kw_end == std::string_view::npos) {
      std::cerr << "Invalid HTTP header. Couldn't parse keyword.\n";
      return;
    }
    keyword = request_line.substr(0, kw_end);

    // Endpoint                                         GET /index.html HTTP1.1
    std::size_t ep_end = request_line.find(' ', kw_end + 1); //        ^
    if (ep_end == std::string_view::npos) {
      std::cerr << "Invalid HTTP header. Couldn't parse endpoint.\n";
      return;
    }
    endpoint = request_line.substr(kw_end + 2,
                                   ep_end - (kw_end + 2)); // skip leading slash

    version = request_line.substr(ep_end + 1);
    connection = field(raw, "Connection");
    accept = field(raw, "Accept");
    fail = false;
  }
};

#endif
