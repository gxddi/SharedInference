#!/usr/bin/env python3
"""A minimal chat terminal for the project's OpenAI-compatible API."""

import argparse
from getpass import getpass
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request(base_url, key, path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = Request(base_url.rstrip("/") + path, data=body, headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    })
    try:
        with urlopen(req, timeout=300) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            message = json.load(exc)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = exc.reason
        raise RuntimeError(f"HTTP {exc.code}: {message}") from None
    except (URLError, OSError) as exc:
        raise RuntimeError(f"Cannot reach {base_url}: {exc}") from None
    except ValueError:
        raise RuntimeError("The server returned an invalid JSON response.") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://66-42-90-154.sslip.io:1025/api/v1",
                        help="API base URL (default: https://66-42-90-154.sslip.io:1025/api/v1)")
    args = parser.parse_args()

    key = getpass("API key (from /keys/): ").strip()
    if not key:
        raise RuntimeError("An API key is required.")

    models = request(args.base_url, key, "/models")["data"]
    if not models:
        raise RuntimeError("No models are ready. Connect nodes and load a model first.")
    print("Available models:")
    for number, model in enumerate(models, start=1):
        print(f"  {number}. {model['id']}")
    while True:
        choice = input("Select a model number (or /quit): ").strip()
        if choice == "/quit":
            return
        try:
            number = int(choice)
        except ValueError:
            number = 0
        if 1 <= number <= len(models):
            model = models[number - 1]["id"]
            break
        print(f"Enter a number from 1 to {len(models)}.")

    print(f"Model: {model}\nType /quit to exit.\n")
    messages = []
    while True:
        prompt = input("You: ").strip()
        if prompt == "/quit":
            return
        if not prompt:
            continue
        conversation = messages + [{"role": "user", "content": prompt}]
        try:
            response = request(args.base_url, key, "/chat/completions", {
                "model": model, "messages": conversation, "max_tokens": 256,
                "stream": False,
            })
            answer = response["choices"][0]["message"]["content"]
            if not isinstance(answer, str):
                raise ValueError
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            continue
        except (KeyError, IndexError, TypeError, ValueError):
            print("Error: The server returned an invalid chat response.", file=sys.stderr)
            continue
        messages = conversation + [{"role": "assistant", "content": answer}]
        print(f"Assistant: {answer}\n")


if __name__ == "__main__":
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
