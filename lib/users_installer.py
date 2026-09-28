"""
FreedomNAS Installer — user creation.

Users are created before packages are installed, so PAM/Samba
authentication is already in place when those services first start.

Accounts are created through the target engine, which means `useradd` on a
Linux target and `pw useradd` on FreeBSD without this module knowing the
difference. Passwords are piped to stdin (`users.set_password_stdin`,
payload shaped by `users.password_stdin_format`) so they never appear in
argv, in ps output, or in the installer log.
"""

from __future__ import annotations

from typing import List

from context import ExecutionContext
from installer_modules import Logger, DialogManager, Configuration

_MIN_PASSWORD_LENGTH = 8
_CONTROL_CHARS = "\n\r\0"


def _credentials_problem(username: str, password: str) -> str:
    if ":" in username or any(c in username for c in _CONTROL_CHARS):
        return "The username may not contain ':' or control characters."
    if any(c in password for c in _CONTROL_CHARS):
        return "The password may not contain line breaks or control characters."
    return ""


class UsersInstaller:
    """Prompts for credentials and creates the accounts in the target system."""

    def __init__(self, dialog: DialogManager, config: Configuration, ctx: ExecutionContext) -> None:
        self.dialog = dialog
        self.config = config
        self.ctx = ctx

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    def prompt_admin_user(self) -> None:
        """Ask for the admin account name, then its password (twice)."""
        username = self.dialog.inputbox(
            "Administrator Account",
            "Enter the name of the FreedomNAS administrator account:",
            init_text=self.config.username,
            height=10,
            width=60,
        )
        if username and username.strip() and not _credentials_problem(username.strip(), ""):
            self.config.username = username.strip()

        self.config.user_password = self._prompt_password(self.config.username)
        Logger.info(f"Admin account: {self.config.username}")

    def prompt_additional_users(self) -> None:
        """Optionally queue extra accounts."""
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
            problem = _credentials_problem(username, "")
            if problem:
                self.dialog.msgbox("Invalid Username", problem)
                continue

            wants_sudo = self.dialog.yesno(
                "Administrative Access",
                f"Grant administrative (sudo) access to '{username}'?",
                height=8,
                width=55,
            )

            self.config.additional_users.append({
                "username": username,
                "password": self._prompt_password(username),
                "sudo": wants_sudo,
            })
            Logger.info(f"Additional user queued: {username}")

    def _prompt_password(self, username: str) -> str:
        while True:
            first = self.dialog.passwordbox(
                "Password", f"Set password for '{username}':", height=10, width=60
            )
            if not first:
                self.dialog.msgbox("Password Required", "A password is required.")
                continue

            second = self.dialog.passwordbox(
                "Confirm Password", f"Confirm password for '{username}':", height=10, width=60
            )
            if first != second:
                self.dialog.msgbox("Password Mismatch", "The passwords do not match.")
                continue

            problem = _credentials_problem(username, first)
            if problem:
                self.dialog.msgbox("Invalid Password", problem)
                continue

            if len(first) < _MIN_PASSWORD_LENGTH:
                self.dialog.msgbox(
                    "Password Too Short",
                    f"The password must be at least {_MIN_PASSWORD_LENGTH} characters long.",
                )
                continue

            return first

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create_users(self) -> None:
        """Create the admin account and any additional accounts in the target."""
        sudo_group = self.ctx.target.get_profile_value("users", "sudo_group")

        self._create_user(
            self.config.username, self.config.user_password or "", groups=[sudo_group]
        )
        for user in self.config.additional_users:
            groups = [sudo_group] if user.get("sudo") else []
            self._create_user(user["username"], user["password"], groups=groups)

    def set_samba_passwords(self) -> None:
        """
        Give every account created here the same password in Samba's own
        database, without which it cannot open a share. Runs after the
        packages, since Samba is not there when the accounts are created.
        """
        if not self.ctx.has("samba", "check_installed"):
            return
        try:
            self.ctx.target_run("samba", "check_installed")
        except Exception:
            Logger.info("Samba is not installed: no Samba passwords to set")
            return
        accounts = [(self.config.username, self.config.user_password or "")]
        accounts += [(u["username"], u["password"]) for u in self.config.additional_users]
        payload = self.ctx.target.get_profile_value("samba", "password_stdin_format")
        for username, password in accounts:
            Logger.info(f"Setting the Samba password of {username}")
            self.ctx.target_run_stdin(
                "samba", "set_user_password",
                payload.format(username=username, password=password),
                username=username,
            )

    def lock_root(self) -> None:
        """
        Lock root on a freshly installed system, when the edition says how.

        FreeBSD's base.txz ships root with an empty password, so the console
        would log anyone in as root; debootstrap and the Linux stage tarballs
        leave it locked already, and declare nothing. The administrator uses
        sudo. Only for a clean install: a running system's root is its
        owner's business.
        """
        if self.ctx.has("users", "lock_root"):
            Logger.info("Locking the root account")
            self.ctx.target_run("users", "lock_root")

    def _create_user(self, username: str, password: str, groups: List[str]) -> None:
        problem = _credentials_problem(username, password)
        if problem:
            raise ValueError(f"{username!r}: {problem}")
        Logger.info(f"Creating user: {username}")

        if groups:
            self.ctx.target_run(
                "users", "create", username=username, groups=",".join(groups)
            )
        else:
            self.ctx.target_run("users", "create_basic", username=username)

        payload = self.ctx.target.get_profile_value("users", "password_stdin_format")
        self.ctx.target_run_stdin(
            "users", "set_password_stdin",
            payload.format(username=username, password=password),
            username=username,
        )
        Logger.info(f"User created: {username}")
