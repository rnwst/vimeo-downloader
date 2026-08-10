# Releasing

Releases are published to PyPI through GitHub Trusted Publishing. No PyPI API
token is stored in the repository.

## One-time configuration

1. Create a PyPI account, enable two-factor authentication, and add a pending
   trusted publisher for the `vimeo-playlist-downloader` project.
2. Configure the publisher with owner `rnwst`, repository
   `vimeo-downloader`, workflow `publish.yml`, and environment `pypi`.
3. Create a protected GitHub environment named `pypi`. Add required reviewers
   if releases should require manual approval.

## Release process

1. Set `__version__` in `download_vimeo.py` to the intended PEP 440 version.
2. Update user-facing documentation for any changed behavior.
3. Run the quality and package checks:

   ```sh
   black --check --diff download_vimeo.py
   pylint download_vimeo.py
   pyright
   python -m build
   python -m twine check dist/*
   ```

4. Commit the release changes and create a tag prefixed with `v`, such as
   `v0.1.0` for package version `0.1.0`.
5. Publish a GitHub release for that tag. The `Publish to PyPI` workflow checks
   that the tag and package version agree before publishing.
6. Verify the release from a clean environment:

   ```sh
   pipx install vimeo-playlist-downloader
   vimeo-playlist-downloader --version
   ```
