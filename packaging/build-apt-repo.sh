#!/usr/bin/env bash
# Build the signed apt repository published on GitHub Pages.
#
# Puts every .deb in <debs-dir> into a repository at <site-dir>, signed with the
# armored private key in APT_SIGNING_KEY. The public key is written alongside it
# as lxcwatch.asc. Needs apt-ftparchive (apt-utils) and gpg.
#
#   APT_SIGNING_KEY="$(cat key.asc)" packaging/build-apt-repo.sh <debs-dir> <site-dir>
#
# The package is Architecture: all, but it only bundles wheels for amd64 and
# arm64, so the repository lists those two.
set -euo pipefail

debs=$(cd "$1" && pwd)
mkdir -p "$2"
site=$(cd "$2" && pwd)
ARCHES="amd64 arm64"

GNUPGHOME=$(mktemp -d)
export GNUPGHOME
trap 'rm -rf "$GNUPGHOME"' EXIT
printf '%s\n' "$APT_SIGNING_KEY" | gpg --batch --quiet --import

cd "$site"
mkdir -p pool/main/l/lxcwatch
cp "$debs"/*.deb pool/main/l/lxcwatch/

for arch in $ARCHES; do
  dir="dists/stable/main/binary-$arch"
  mkdir -p "$dir"
  apt-ftparchive packages pool > "$dir/Packages"
  gzip -9nkf "$dir/Packages"
done

# Written elsewhere first: apt-ftparchive would otherwise list Release in itself.
release=$(mktemp)
apt-ftparchive \
  -o APT::FTPArchive::Release::Origin=lxcwatch \
  -o APT::FTPArchive::Release::Label=lxcwatch \
  -o APT::FTPArchive::Release::Suite=stable \
  -o APT::FTPArchive::Release::Codename=stable \
  -o APT::FTPArchive::Release::Architectures="$ARCHES" \
  -o APT::FTPArchive::Release::Components=main \
  release dists/stable > "$release"
mv "$release" dists/stable/Release
chmod 644 dists/stable/Release

gpg --batch --yes --armor --detach-sign -o dists/stable/Release.gpg dists/stable/Release
gpg --batch --yes --clearsign -o dists/stable/InRelease dists/stable/Release
gpg --batch --yes --armor --export -o lxcwatch.asc
