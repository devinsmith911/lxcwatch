# Releasing

Pushes to `main` only run the tests. A release is made by pushing a version tag.

1. In [CHANGELOG.md](CHANGELOG.md), rename the `## Unreleased` section to `## X.Y.Z (YYYY-MM-DD)`, or add that entry if there's no Unreleased section, and commit it to `main`. Between releases, note user-facing changes under `## Unreleased`.
2. Tag that commit with the plain version, with no `v` prefix, and push the tag:

   ```sh
   git tag X.Y.Z && git push origin X.Y.Z
   ```

The **Release** workflow then:

1. checks that the tag is `X.Y.Z` and that the changelog has an entry for it;
2. tests the tagged commit;
3. builds `lxcwatch_X.Y.Z_all.deb` and publishes it to the apt repository (distribution `stable`, component `main`);
4. creates the release, titled `X.Y.Z`, with that version's changelog section as the notes and the `.deb` attached.

To create or refresh the release for an existing tag, run the workflow by hand: **Actions → Release → Run workflow**, then enter the tag. A version that's already in the apt repository is left as it is.

The workflow needs a `PACKAGES_TOKEN` repository secret: a token with package read and write access. The built-in CI token can create releases but can't publish packages.
