# Maintainer: FreedomNAS Team <dev@freedomnas.io>
pkgname=freedomnas-installer
pkgver=4.0.0
pkgrel=1
pkgdesc="Profile-driven FreedomNAS installer (clean install or in-place)"
arch=('any')
url="https://codeberg.org/freedomnas/freedomnas-installer"
license=('MPL-2.0')
depends=(
    'freedomnas-core'
    'freedomnas-profiles'
    'python'
    'dialog'
    'gptfdisk'
    'dosfstools'
    'zfs-utils'
    'dracut'
    'efibootmgr'
    'curl'
)
optdepends=('debootstrap: bootstrap a Debian target natively')
source=("git+https://codeberg.org/freedomnas/freedomnas-installer.git#tag=v${pkgver}")
sha256sums=('SKIP')

package() {
    cd "${srcdir}/${pkgname}"

    # Install the installer to /usr/lib/freedomnas/installer
    install -Dm 755 setup.py "${pkgdir}/usr/lib/freedomnas/installer/setup.py"
    mkdir -p "${pkgdir}/usr/lib/freedomnas/installer/lib"
    install -Dm 644 lib/*.py "${pkgdir}/usr/lib/freedomnas/installer/lib/"

    # Boot-chain assets (Clover, zz-dracut-uki) come from freedomnas-profiles

    # User-facing entry point
    mkdir -p "${pkgdir}/usr/bin"
    ln -sf /usr/lib/freedomnas/installer/setup.py "${pkgdir}/usr/bin/freedomnas-install"

    # Install license if present
    [ -f LICENSE ] && install -Dm 644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"
    [ -f README.md ] && install -Dm 644 README.md "${pkgdir}/usr/share/doc/${pkgname}/README.md"
}
