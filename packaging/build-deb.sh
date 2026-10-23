#!/usr/bin/env bash
# Build lxcwatch_<version>_all.deb into dist/.
#
# The package carries wheels for lxcwatch and its dependencies for CPython
# 3.11-3.13 (Debian 12, Ubuntu 24.04, Debian 13) on amd64 and arm64; postinst
# builds /opt/lxcwatch/venv from them offline. Needs python3 with pip, git and
# dpkg-deb.
#
#   VERSION=0.1.42 packaging/build-deb.sh
#
# Releases are built by CI from an X.Y.Z tag. Without VERSION, a local build
# is labelled after the latest tag, e.g. 0.1.36+dev3.g1a2b3c4.
set -euo pipefail

cd "$(dirname "$0")/.."
if [ -z "${VERSION:-}" ]; then
  desc=$(git describe --tags --match '[0-9]*.[0-9]*.[0-9]*' --long 2>/dev/null || echo "0.0.0-$(git rev-list --count HEAD)-g$(git rev-parse --short HEAD)")
  tag=${desc%-*-g*}; rest=${desc#"$tag"-}
  VERSION="$tag+dev${rest%%-*}.${rest#*-}"
fi
PYTHONS="3.11 3.12 3.13"
ARCHES="x86_64 aarch64"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
root="$work/root"
wheels="$root/usr/lib/lxcwatch/wheels"
mkdir -p "$wheels" dist

# lxcwatch itself, stamped with the package version.
cp -r pyproject.toml lxcwatch "$work/"
sed -i.bak "s/^version = \".*\"/version = \"$VERSION\"/" "$work/pyproject.toml"
printf '__version__ = "%s"\n' "$VERSION" > "$work/lxcwatch/__init__.py"
python3 -m pip wheel -q --no-deps -w "$wheels" "$work"

# Dependencies for every supported Python and architecture.
deps=$(python3 -c 'import tomllib; print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]))')
for py in $PYTHONS; do
  for arch in $ARCHES; do
    # shellcheck disable=SC2086
    python3 -m pip download -q -d "$wheels" --only-binary=:all: \
      --python-version "$py" --implementation cp \
      --platform "manylinux_2_17_$arch" --platform "manylinux2014_$arch" --platform "manylinux_2_28_$arch" \
      $deps
  done
done

install -Dm644 deploy/lxcwatch.service "$root/lib/systemd/system/lxcwatch.service"
install -Dm644 .env.example "$root/usr/share/lxcwatch/lxcwatch.env.example"
install -Dm644 README.md "$root/usr/share/doc/lxcwatch/README.md"
install -Dm644 CHANGELOG.md "$root/usr/share/doc/lxcwatch/CHANGELOG.md"

mkdir -p "$root/DEBIAN"
for f in postinst prerm postrm; do
  install -m755 "packaging/debian/$f" "$root/DEBIAN/$f"
done
size=$(du -sk "$root" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$size/" packaging/debian/control > "$root/DEBIAN/control"

out="dist/lxcwatch_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$root" "$out" >/dev/null
echo "$out"
