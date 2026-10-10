# Contributing

Contributions are welcome under the Developer Certificate of Origin (DCO).

## Sign your work

Every commit must be signed to certify you wrote the contribution or have the right to submit it under the project's license:

```
git commit -s -m "your message"
```

This adds a `Signed-off-by: Name <email>` trailer. We cannot accept commits without it. See [developercertificate.org](https://developercertificate.org/) for the full text.

## Pull requests

- Keep changes focused; explain the *why* in the PR description.
- Run the test suite locally before opening a PR (`python -m unittest discover -s tests -p "test_*.py"`).
- For security issues, do not open a public issue — see SECURITY.md.
