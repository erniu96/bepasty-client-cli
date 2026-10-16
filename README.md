bepasty-client-cli
==================

Command line client for bepasty (binary pastebin)

This fork improves chunked uploads and compatibility. It does **not** implement
browser-side encrypted shares or resumable browser uploads; CLI uploads remain
ordinary REST uploads.

Install `requirements.txt` into a virtual environment, then run
`bash command.txt --help`. The launcher uses `python3`; set `PYTHON` to select
another interpreter. No files outside this repository are required by the launcher.

Upload performance
------------------

Uploads reuse an HTTP session across chunks, allowing the server and any reverse
proxy to keep the TCP connection (and TLS connection for HTTPS) open. Connection
reuse depends on the server and proxy supporting keep-alive.

The client reads at most 1 MiB of input per chunk. The REST protocol uses Base64,
so a full chunk has a request body of 1,398,104 bytes. Configure a reverse proxy's
request body limit to accept this size, for example 2 MiB. The server's
`MAX_BODY_SIZE` configures browser upload chunks; it does not change CLI chunks.

Piped input uses a one-byte lookahead to detect the final chunk, including inputs
whose size is an exact multiple of 1 MiB. This keeps reads bounded and marks the
upload complete when stdin reaches EOF. The current REST byte-range protocol
cannot represent an empty upload, so empty input exits with an error.

MIME detection accepts both bytes and text returned by different versions of
`python-magic`. Startup also works on platforms without the Unix `pwd` module.
HTTP upload failures exit nonzero, so shell scripts do not mistake them for success.

Tests
-----

After installing the dependencies from `requirements.txt`, run:

```
python -m unittest discover -s tests -v
```

The tests include a local HTTP/1.1 server to check that multiple upload chunks
share one TCP connection and preserve the REST upload protocol. They also cover
piped input at chunk boundaries, empty input, and both MIME return types.
