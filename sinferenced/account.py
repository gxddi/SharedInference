"""Talking to the site: the HTTPS client, saved credentials, browser sign-in, and the account's wallet."""

import json
import os
import select
import ssl
import sys
import tempfile
import termios
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class ClientError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status  # HTTP status, when the server sent one


def origin(value):
    """Check that value is a bare origin like https://localhost:1025."""
    parsed = urlsplit(value)

    if (parsed.scheme not in ("https", "http") or not parsed.netloc
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise ClientError("Server must be an HTTPS origin, such as https://localhost:1025.")

    # Plain HTTP only for local development
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ClientError("Plain HTTP is only allowed on loopback for local development.")

    return value.rstrip("/")


class Client:
    def __init__(self, server, ca_file=None):
        self.server = origin(server)
        self.context = ssl.create_default_context(cafile=ca_file)  # also used for the node connection

    def request(self, path, data=None, token=None):
        """POST data (or GET without it) to /api/<path> and return the JSON reply."""
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        body = json.dumps(data).encode() if data is not None else None
        request = Request(self.server + "/api" + path, data=body, headers=headers)

        try:
            with urlopen(request, timeout=15, context=self.context) as response:
                return json.load(response)

        # Server answered w/ an error
        except HTTPError as exc:
            try:
                message = json.load(exc).get("message", "Request failed.")
            except (ValueError, AttributeError):
                message = f"Server returned HTTP {exc.code}."
            raise ClientError(message, exc.code) from None

        # Server never answered
        except (URLError, TimeoutError) as exc:
            raise ClientError(f"Cannot reach {self.server}: {exc}. Check the server and trusted certificate.") from None
        except ValueError:
            raise ClientError("Server returned an invalid response.") from None


###############
# Credentials #
###############

def load_config(path):
    """Saved credentials, or None when this computer isn't signed in."""
    try:
        config = json.loads(path.read_text())
        if not isinstance(config, dict):
            raise ValueError
        if not all(isinstance(config.get(key), str) for key in ("server", "access_token", "username")):
            raise ValueError
        return config

    except FileNotFoundError:
        return None
    except (ValueError, OSError):
        raise ClientError(f"Cannot read credentials in {path}. Remove the file and sign in again.") from None


def save_config(path, config):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Write a temp file, then swap it in so a crash never leaves half a file
    descriptor, name = tempfile.mkstemp(prefix=".credentials-", dir=path.parent)  # user-only (0600)
    try:
        with os.fdopen(descriptor, "w") as out:
            json.dump(config, out)
            out.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


###################
# Browser sign-in #
###################

def login(client, path, device_name):
    """Print a sign-in link, wait for approval in the browser, then save the node session."""
    device = client.request("/auth/device/start", {"device_name": device_name})
    url = device["verification_uri_complete"]

    # Only show a link on the server the user chose
    if urlsplit(url)[:2] != urlsplit(client.server)[:2]:
        raise ClientError("Server returned a sign-in link on a different origin.")

    print(f"Open this link to sign in or create an account:\n\n  {url}\n")
    print(f"Confirm code: {device['user_code']}\nDevice: {device_name}", flush=True)
    print("Waiting for browser approval…", flush=True)

    # Poll until approved (or the login expires)
    deadline = time.monotonic() + device["expires_in"]
    interval = device["interval"]
    while time.monotonic() < deadline:
        time.sleep(interval)
        result = client.request("/auth/device/token", {"device_code": device["device_code"]})

        if result.get("error") == "authorization_pending":
            continue
        if result.get("error") == "slow_down":
            interval = max(interval + 2, result["interval"])
            continue
        if "access_token" not in result:
            raise ClientError(result.get("message", "Could not finish sign-in."))

        config = {"server": client.server, "access_token": result["access_token"], "username": result["username"]}
        save_config(path, config)
        print(f"Signed in as {config['username']}.")
        return config

    raise ClientError("Sign-in timed out. Run sinference login again.")


def ensure_wallet(client, config):
    """The Solana wallet all the account's nodes stake from + get paid to, if the server wants one: its
    private key pasted here, or added on the nodes page in a browser, whichever comes first."""
    token = config["access_token"]
    state = client.request("/nodes/wallet", token=token)
    if not state["needed"] or state["address"]:
        return

    print(f"Your nodes need a Solana wallet (devnet) to stake from and get paid to.\n"
          f"Paste its private key here (it won't show), or add it at {client.server}/nodes/\n", flush=True)
    typing = sys.stdin.isatty()
    shown = termios.tcgetattr(sys.stdin) if typing else None
    try:
        if typing:  # the key stays off the screen
            hidden = termios.tcgetattr(sys.stdin)
            hidden[3] &= ~termios.ECHO
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, hidden)

        while True:
            # Pasted here?
            if typing and select.select([sys.stdin], [], [], 3)[0]:
                key = sys.stdin.readline().strip()
                print()
                try:
                    address = client.request("/nodes/wallet", {"key": key}, token)["address"]
                    print(f"Wallet {address} added.", flush=True)
                    return
                except ClientError as exc:
                    print(f"{exc} Try again:", flush=True)
                    continue
            elif not typing:
                time.sleep(3)

            # ...or added in the browser
            address = client.request("/nodes/wallet", token=token)["address"]
            if address:
                print(f"Wallet {address} added in the browser.", flush=True)
                return
    finally:
        if shown:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, shown)
