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

The site is uploaded in the `pages` job and deployed in a separate `deploy` job, which retries once after 30 seconds if the new artifact isn't visible yet ([actions/deploy-pages#451](https://github.com/actions/deploy-pages/issues/451)). If the deploy still fails, use **Re-run failed jobs**: that re-runs only `deploy`, against the artifact that's already there. Don't re-run `pages` on its own: a second `github-pages` artifact in the same run makes the deploy fail with `Multiple artifacts named "github-pages"`. In that case run the workflow by hand instead. That starts clean, but it rebuilds the `.deb` and replaces the one on the release.

The workflow needs an `APT_SIGNING_KEY` repository secret: the armored private key that signs the apt repository. Users trust its public half, so replacing the key means every install has to fetch the new one. Pages must be set to deploy from GitHub Actions, and the `github-pages` environment must allow `[0-9]*` tags as well as `main`.
