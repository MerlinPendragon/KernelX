# Stable bootstrap compatibility fixture

Unmodified `kernelx/bootstrap.py` and `kernelx/release.py` from main commit
`11d004d72056416134bd0d7b91588f7573f61421` (PR #10).

The regression imports these original implementations in a separate process
and verifies and installs a release built from the current checkout.

SHA-256:

- bootstrap.py: `a289b0c1eb61ea5cecea927cba8a00593a5bff8d4bf1bfa8f48eaa68fc6aaee7`
- release.py: `7d99c77257772800c4387e81df126f759e5fc8fbfedbf81f72a256a61ccd38d6`
