# =============================================================================
# System Installation and Configuration
# =============================================================================

import os
from typing import Optional
from installer_modules import (
    Logger, DialogManager, Configuration, run_command, 
    run_command_chroot, write_file, directory_exists,
    run_command_safe  # Use safe command execution
)


class SystemInstaller:
    """Handle system installation and configuration"""
    
    MIRRORS = {
        "US": ("United States (deb.debian.org)", "http://deb.debian.org/debian"),
        "UK": ("United Kingdom (ftp.uk.debian.org)", "http://ftp.uk.debian.org/debian"),
        "DE": ("Germany (ftp.de.debian.org)", "http://ftp.de.debian.org/debian"),
        "FR": ("France (ftp.fr.debian.org)", "http://ftp.fr.debian.org/debian"),
        "NL": ("Netherlands (ftp.nl.debian.org)", "http://ftp.nl.debian.org/debian"),
        "AU": ("Australia (ftp.au.debian.org)", "http://ftp.au.debian.org/debian"),
        "JP": ("Japan (ftp.jp.debian.org)", "http://ftp.jp.debian.org/debian"),
        "CA": ("Canada (ftp.ca.debian.org)", "http://ftp.ca.debian.org/debian"),
        "BR": ("Brazil (ftp.br.debian.org)", "http://ftp.br.debian.org/debian"),
        "IT": ("Italy (ftp.it.debian.org)", "http://debian.connesi.it/debian"),
        "ES": ("Spain (ftp.es.debian.org)", "http://ftp.es.debian.org/debian"),
        "SE": ("Sweden (ftp.se.debian.org)", "http://ftp.se.debian.org/debian"),
        "CH": ("Switzerland (ftp.ch.debian.org)", "http://ftp.ch.debian.org/debian"),
        "PL": ("Poland (ftp.pl.debian.org)", "http://ftp.pl.debian.org/debian"),
    }
    
    def __init__(self, logger: Logger, dialog: DialogManager, config: Configuration):
        self.logger = logger
        self.dialog = dialog
        self.config = config
    
    def detect_boot_mode(self):
        """Detect UEFI or BIOS boot mode"""
        self.logger.info("Detecting boot mode...")
        
        if directory_exists('/sys/firmware/efi'):
            self.config.boot_mode = "UEFI"
            self.logger.info("Detected UEFI boot mode")
        else:
            self.config.boot_mode = "BIOS"
            self.logger.info("Detected BIOS boot mode")
    
    def select_mirror(self):
        """Select Debian mirror by country/region"""
        self.logger.info("Selecting Debian mirror...")
        
        # Build menu items
        items = [(key, label) for key, (label, _) in self.MIRRORS.items()]
        items.append(("CUSTOM", "Custom (enter your own mirror URL)"))
        
        choice = self.dialog.menu(
            "Debian Mirror Selection",
            "Select the Debian mirror closest to your location:",
            items,
            height=22,
            width=78
        )
        
        if not choice:
            self.logger.warn("User cancelled mirror selection, using default US mirror")
            self.config.debian_mirror = "http://deb.debian.org/debian"
            return
        
        if choice == "CUSTOM":
            custom_url = self.dialog.inputbox(
                "Custom Mirror",
                "Enter the Debian mirror URL:",
                init_text="http://deb.debian.org/debian",
                height=10,
                width=70
            )
            if custom_url:
                self.config.debian_mirror = custom_url
            else:
                self.logger.warn("Custom mirror cancelled, using default")
                self.config.debian_mirror = "http://deb.debian.org/debian"
        elif choice in self.MIRRORS:
            _, url = self.MIRRORS[choice]
            self.config.debian_mirror = url
        else:
            self.logger.warn("Invalid choice, using default US mirror")
            self.config.debian_mirror = "http://deb.debian.org/debian"
        
        self.logger.info(f"Selected mirror: {self.config.debian_mirror}")
    
    def prompt_hostname(self):
        """Prompt the user for the system hostname (spec §8.3 step 4)."""
        self.logger.info("Prompting for hostname...")
        hostname = self.dialog.inputbox(
            "System Hostname",
            "Enter the hostname for this FreedomNAS system:\n\n"
            "(letters, numbers, and hyphens only; no spaces)",
            init_text=self.config.hostname,
            height=12,
            width=60,
        )
        if hostname and hostname.strip():
            self.config.hostname = hostname.strip()
        self.logger.info(f"Hostname set to: {self.config.hostname}")

    def install_base_system(self):
        """Install Debian base system using debootstrap"""
        self.logger.info("Installing Debian base system...")
        
        # Check if debootstrap is available
        try:
            run_command("which debootstrap", check=True)
        except:
            self.logger.info("debootstrap not found, installing...")
            run_command("apt-get update", check=True)
            run_command("apt-get install -y debootstrap", check=True)
        
        # Run debootstrap
        self.logger.info(f"Running debootstrap with mirror: {self.config.debian_mirror}")
        
        try:
            run_command(
                f'debootstrap --arch amd64 trixie /mnt "{self.config.debian_mirror}"',
                check=True
            )
            self.logger.info("Base system installed successfully")
        except Exception as e:
            self.logger.error(f"debootstrap failed: {e}")
            raise RuntimeError("Base system installation failed")
    
    def configure_system(self):
        """Configure system hostname, hosts, and fstab"""
        self.logger.info("Configuring system...")
        
        # Set hostname
        hostname_path = "/mnt/etc/hostname"
        write_file(hostname_path, f"{self.config.hostname}\n")
        
        # Configure /etc/hosts
        fqdn = f"{self.config.hostname}.{self.config.domain}" if self.config.domain else self.config.hostname
        
        hosts_content = f"""127.0.0.1       localhost
127.0.1.1       {fqdn} {self.config.hostname}

# The following lines are desirable for IPv6 capable hosts
::1     localhost ip6-localhost ip6-loopback
ff02::1 ip6-allnodes
ff02::2 ip6-allrouters
"""
        write_file("/mnt/etc/hosts", hosts_content)
        
        # Configure /etc/fstab
        if self.config.boot_mode == "UEFI":
            fstab_content = f"""# /etc/fstab: static file system information.
#
# <file system> <mount point>   <type>  <options>       <dump>  <pass>
# EFI System Partition
{self.config.part2}          /boot/efi       vfat    defaults        0       2
"""
        else:
            fstab_content = """# /etc/fstab: static file system information.
#
# <file system> <mount point>   <type>  <options>       <dump>  <pass>
# ZFS datasets are mounted by ZFS, not fstab
"""
        
        write_file("/mnt/etc/fstab", fstab_content)
        
        self.logger.info("System configured")
    
    def setup_chroot(self):
        """Setup chroot environment with necessary mounts"""
        self.logger.info("Setting up chroot environment...")
        
        try:
            # Mount necessary filesystems for chroot
            mounts = [
                ("/dev", "/mnt/dev"),
                ("/dev/pts", "/mnt/dev/pts"),
                ("/proc", "/mnt/proc"),
                ("/sys", "/mnt/sys"),
                ("/run", "/mnt/run"),
            ]
            
            for source, target in mounts:
                run_command(f"mount --bind {source} {target}", check=True)
                self.logger.info(f"Mounted {source} to {target}")
            
            # Copy resolv.conf for DNS resolution in chroot
            run_command("cp /etc/resolv.conf /mnt/etc/resolv.conf", check=True)
            
            self.logger.info("Chroot environment ready with all required mounts")
        except Exception as e:
            self.logger.error(f"Chroot setup failed: {e}")
            raise
