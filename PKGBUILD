# Maintainer: FreedomNAS Team <dev@freedomnas.io>
pkgname=freedomnas-installer
pkgver=4.0.0
pkgrel=1
pkgdesc="FreedomNAS ZFS-on-root installer"
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
    'debootstrap'
    'dracut'
    'efibootmgr'
)
source=("git+https://codeberg.org/freedomnas/freedomnas-installer.git#tag=v${pkgver}")
sha256sums=('SKIP')

package() {
    cd "${srcdir}/${pkgname}"

    # Install the installer to /usr/lib/freedomnas/installer
    install -Dm 755 setup.py "${pkgdir}/usr/lib/freedomnas/installer/setup.py"
    mkdir -p "${pkgdir}/usr/lib/freedomnas/installer/lib"
    install -Dm 644 lib/*.py "${pkgdir}/usr/lib/freedomnas/installer/lib/"

    # assets/ contains vendored Clover binaries and the zz-dracut-uki hook
    mkdir -p "${pkgdir}/usr/lib/freedomnas/installer/assets"
    cp -r assets/. "${pkgdir}/usr/lib/freedomnas/installer/assets/"
    chmod +x "${pkgdir}/usr/lib/freedomnas/installer/assets/zz-dracut-uki"

    # User-facing entry point
    mkdir -p "${pkgdir}/usr/bin"
    ln -sf /usr/lib/freedomnas/installer/setup.py "${pkgdir}/usr/bin/freedomnas-install"

    # Install license if present
    [ -f LICENSE ] && install -Dm 644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"
    [ -f README.md ] && install -Dm 644 README.md "${pkgdir}/usr/share/doc/${pkgname}/README.md"
}
