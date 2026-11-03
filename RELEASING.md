# Releasing

A release is made by pushing a version tag to GitHub. Pushes to `main` don't run anything.

1. In [CHANGELOG.md](CHANGELOG.md), rename the `## Unreleased` section to `## X.Y.Z (YYYY-MM-DD)`, or add that entry if there's no Unreleased section, and commit it to `main`. Between releases, note user-facing changes under `## Unreleased`.
2. Tag that commit with the plain version, with no `v` prefix, and push the tag:

   ```sh
   git tag X.Y.Z && git push origin X.Y.Z
   ```

The **Release** workflow then:

1. checks that the tag is `X.Y.Z` and that the changelog has an entry for it;
2. tests the tagged commit;
3. builds `lxcwatch_X.Y.Z_all.deb`;
4. creates the GitHub release, titled `X.Y.Z`, with that version's changelog section as the notes and the `.deb` attached;
5. rebuilds the public apt repository on GitHub Pages from the five latest releases.

To create or refresh the release for an existing tag, run the workflow by hand: **Actions → Release → Run workflow**, then enter the tag.

The workflow needs an `APT_SIGNING_KEY` repository secret: the armored private key that signs the apt repository. Users trust its public half, so replacing the key means every install has to fetch the new one. Pages must be set to deploy from GitHub Actions, and the `github-pages` environment must allow `[0-9]*` tags as well as `main`.
