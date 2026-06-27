"""
FreedomNAS Installer — User creation and password prompts.

Steps: get_admin_password (step 12), prompt_additional_users (step 13).
Both steps run BEFORE install_packages so the users exist when packages
set up PAM/Samba authentication.

All password handling uses execute_with_stdin (via engine) to avoid
exposing passwords in command strings — spec §13.2.
"""

from __future__ import annotations

from typing import Optional

from installer_modules import Logger, DialogManager, Configuration, run_in_chroot


class UsersInstaller:
    """Prompts for user credentials and creates OS accounts inside the chroot."""

    def __init__(self, logger: Logger, dialog: DialogManager, config: Configuration) -> None:
        self.logger = logger
        self.dialog = dialog
        self.config = config

    # ------------------------------------------------------------------
    # Public steps
    # ------------------------------------------------------------------

    def get_admin_password(self) -> None:
        """
        Double-prompt for the admin user password (spec §8.3 step 12).
        Loops until the two entries match.
        """
        while True:
            pwd1 = self.dialog.passwordbox(
                "Admin Password",
                f"Set password for user '{self.config.username}':",
                height=10,
                width=60,
            )
            if not pwd1:
                self.dialog.msgbox(
                    "Password Required",
                    "A password is required for the admin account.\n"
                    "Please try again.",
                )
                continue

            pwd2 = self.dialog.passwordbox(
                "Confirm Password",
                f"Confirm password for '{self.config.username}':",
                height=10,
                width=60,
            )

            if pwd1 != pwd2:
                self.dialog.msgbox(
                    "Password Mismatch",
                    "The passwords do not match. Please try again.",
                )
                continue

            if len(pwd1) < 8:
                self.dialog.msgbox(
                    "Password Too Short",
                    "The password must be at least 8 characters long.",
                )
                continue

            self.config.user_password = pwd1
            Logger.info(f"Admin password set for: {self.config.username}")
            return

    def prompt_additional_users(self) -> None:
        """
        Optionally add extra OS users (spec §8.3 step 13).
        Loops until the user declines to add another.
        """
        while self.dialog.yesno(
            "Additional Users",
            "Do you want to add another user?",
            height=8,
            width=50,
        ):
            username = self.dialog.inputbox(
                "Username",
                "Enter the new username (lowercase letters, digits, underscores):",
                height=10,
                width=60,
            )
            if not username or not username.strip():
                continue
            username = username.strip()

            wants_sudo = self.dialog.yesno(
                "Sudo Access",
                f"Grant sudo (wheel group) access to '{username}'?",
                height=8,
                width=55,
            )

            while True:
                pwd1 = self.dialog.passwordbox(
                    "User Password",
                    f"Password for '{username}':",
                    height=10,
                    width=60,
                )
                if not pwd1:
                    self.dialog.msgbox("Password Required", "A password is required.")
                    continue
                pwd2 = self.dialog.passwordbox(
                    "Confirm Password",
                    f"Confirm password for '{username}':",
                    height=10,
                    width=60,
                )
                if pwd1 != pwd2:
                    self.dialog.msgbox("Password Mismatch", "Passwords do not match.")
                    continue
                break

            self.config.additional_users.append({
                "username": username,
                "password": pwd1,
                "sudo": wants_sudo,
            })
            Logger.info(f"Additional user queued: {username}")

    def create_users_in_chroot(self) -> None:
        """
        Create all users inside the installed chroot.
        Called after install_base_system (packages must provide useradd/chpasswd).
        """
        self._create_user(
            self.config.username,
            self.config.user_password or "",
            groups=["sudo", "samba"],
        )
        for user in self.config.additional_users:
            groups = ["sudo"] if user.get("sudo") else []
            self._create_user(user["username"], user["password"], groups=groups)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_user(self, username: str, password: str, groups: list) -> None:
        Logger.info(f"Creating user: {username}")
        run_in_chroot(["useradd", "-m", "-s", "/bin/bash", username])
        for group in groups:
            run_in_chroot(["usermod", "-aG", group, username], check=False)
        # Set password via chpasswd — password never appears in argv
        import subprocess
        subprocess.run(
            ["chroot", "/mnt", "chpasswd"],
            input=f"{username}:{password}\n",
            text=True,
            check=True,
        )
        Logger.info(f"User created: {username}")
