# =============================================================================
# Common Functions and Modules
# =============================================================================

import subprocess
import os
import sys
import tempfile
from typing import Tuple, Optional, List
import shlex
from pathlib import Path
import logging

# Import FreedomNAS Core Engine — try installed package first, fall back to
# the embedded copy in lib/core/ for environments where freedomnas-core is
# not yet installed (e.g. running the installer directly from source).
try:
    from freedomnas.engine import FreedomNASEngine, CommandExecutionError, CommandTranslationError
    CORE_AVAILABLE = True
except ImportError:
    try:
        from core.engine import FreedomNASEngine, CommandExecutionError, CommandTranslationError
        CORE_AVAILABLE = True
    except ImportError:
        CORE_AVAILABLE = False
        FreedomNASEngine = None
        CommandExecutionError = Exception
        CommandTranslationError = Exception


class Logger:
    """Logging utility with colors and file logging"""

    # ANSI color codes
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    NC = '\033[0m'  # No Color

    _file_handler = None
    _logger = None

    @staticmethod
    def _open_log_handler(log_path: str = "/tmp/freedomnas-install.log"):
        """Open log file with fallback paths if primary fails"""
        if Logger._file_handler is not None:
            return Logger._file_handler

        fallback_paths = [
            log_path,
            "/var/log/freedomnas-install.log" if os.geteuid() == 0 else None,
            os.path.expanduser("~/.local/share/freedomnas/install.log"),
        ]
        fallback_paths = [p for p in fallback_paths if p is not None]

        for path in fallback_paths:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                handler = logging.FileHandler(path)
                handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
                print(f"[DEBUG] Logging to: {path}", file=sys.stderr)
                return handler
            except (OSError, PermissionError):
                continue

        try:
            fd, path = tempfile.mkstemp(suffix='.log', prefix='freedomnas-install-')
            os.close(fd)
            handler = logging.FileHandler(path)
            handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
            print(f"[DEBUG] Logging to temp file: {path}", file=sys.stderr)
            return handler
        except Exception:
            print("[DEBUG] Could not open log file, using NullHandler", file=sys.stderr)
            return logging.NullHandler()

    @staticmethod
    def _setup_logger():
        """Setup the logger instance with file and console handlers"""
        if Logger._logger is not None:
            return Logger._logger

        Logger._logger = logging.getLogger('freedomnas-installer')
        Logger._logger.setLevel(logging.DEBUG)

        if not Logger._logger.handlers:
            Logger._file_handler = Logger._open_log_handler()
            Logger._logger.addHandler(Logger._file_handler)
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(logging.Formatter('%(message)s'))
            Logger._logger.addHandler(console_handler)

        return Logger._logger

    @staticmethod
    def info(message: str):
        """Log info message"""
        print(f"{Logger.GREEN}[INFO]{Logger.NC} {message}")
        logger = Logger._setup_logger()
        logger.info(message)

    @staticmethod
    def warn(message: str):
        """Log warning message"""
        print(f"{Logger.YELLOW}[WARN]{Logger.NC} {message}")
        logger = Logger._setup_logger()
        logger.warning(message)

    @staticmethod
    def error(message: str):
        """Log error message"""
        print(f"{Logger.RED}[ERROR]{Logger.NC} {message}")
        logger = Logger._setup_logger()
        logger.error(message)


# =============================================================================
# FreedomNAS Core Engine Integration
# =============================================================================

class EngineManager:
    """Singleton manager for FreedomNASEngine"""
    _instance = None
    
    @classmethod
    def get_engine(cls) -> Optional['FreedomNASEngine']:
        """Get or create the engine instance"""
        if cls._instance is None and CORE_AVAILABLE:
            try:
                cls._instance = FreedomNASEngine()
                Logger.info(f"Engine initialized for {cls._instance.get_os_info()['os_name']}")
            except Exception as e:
                Logger.warn(f"Could not initialize FreedomNAS Engine: {e}")
                cls._instance = None
        return cls._instance
    
    @classmethod
    def reset(cls):
        """Reset the engine instance"""
        cls._instance = None


class DialogManager:
    """
    Supports both dialog (Linux/generic) and whiptail (BSD/lightweight)
    Automatically detects available backend.
    """
    
    _backend = None  # Cache for detected backend
    
    @classmethod
    def _detect_backend(cls) -> str:
        """Detect available dialog backend (dialog or whiptail)"""
        if cls._backend is not None:
            return cls._backend
        
        # Try dialog first (more feature-rich)
        try:
            subprocess.run(['which', 'dialog'], capture_output=True, check=True)
            cls._backend = 'dialog'
            Logger.info("Using dialog for UI")
            return 'dialog'
        except subprocess.CalledProcessError:
            pass
        
        # Fallback to whiptail
        try:
            subprocess.run(['which', 'whiptail'], capture_output=True, check=True)
            cls._backend = 'whiptail'
            Logger.info("Using whiptail for UI")
            return 'whiptail'
        except subprocess.CalledProcessError:
            Logger.error("Neither dialog nor whiptail found")
            raise RuntimeError("UI backend not available")
    
    @staticmethod
    def msgbox(title: str, text: str, height: int = 10, width: int = 70) -> bool:
        """Show a message box"""
        try:
            backend = DialogManager._detect_backend()
            subprocess.run([
                backend,
                '--title', title,
                '--msgbox', text,
                str(height), str(width)
            ], check=False)
            return True
        except Exception as e:
            Logger.error(f"Dialog error: {str(e)}")
            return False
    
    @staticmethod
    def yesno(title: str, text: str, height: int = 10, width: int = 70) -> bool:
        """Show a yes/no dialog"""
        try:
            backend = DialogManager._detect_backend()
            result = subprocess.run([
                backend,
                '--title', title,
                '--yesno', text,
                str(height), str(width)
            ], capture_output=False)
            return result.returncode == 0
        except Exception as e:
            Logger.error(f"Dialog error: {str(e)}")
            return False
    
    @staticmethod
    def defaultno(title: str, text: str, height: int = 10, width: int = 70) -> bool:
        """Show a yes/no dialog with default to 'no'"""
        try:
            backend = DialogManager._detect_backend()
            result = subprocess.run([
                backend,
                '--title', title,
                '--defaultno',
                '--yesno', text,
                str(height), str(width)
            ], capture_output=False)
            return result.returncode == 0
        except Exception as e:
            Logger.error(f"Dialog error: {str(e)}")
            return False
    
    @staticmethod
    def menu(title: str, text: str, items: List[Tuple[str, str]], 
             height: int = 20, width: int = 70) -> Optional[str]:
        """Show a menu dialog"""
        try:
            backend = DialogManager._detect_backend()
            
            with tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.txt') as f:
                for key, label in items:
                    f.write(f"{key}\n{label}\n")
                menufile = f.name
            
            with tempfile.NamedTemporaryFile(mode='r+', delete=False, suffix='.txt') as f:
                outfile = f.name
            
            result = subprocess.run([
                backend,
                '--title', title,
                '--menu', text,
                str(height), str(width), '12',
                '--file', menufile
            ], stdout=open(outfile, 'w'), stderr=subprocess.DEVNULL)
            
            if result.returncode == 0:
                with open(outfile, 'r') as f:
                    choice = f.read().strip()
                os.unlink(menufile)
                os.unlink(outfile)
                return choice
            
            os.unlink(menufile)
            os.unlink(outfile)
            return None
        except Exception as e:
            Logger.error(f"Dialog error: {str(e)}")
            return None
    
    @staticmethod
    def inputbox(title: str, text: str, init_text: str = "", 
                 height: int = 10, width: int = 70) -> Optional[str]:
        """Show an input dialog"""
        try:
            with tempfile.NamedTemporaryFile(mode='r+', delete=False, suffix='.txt') as f:
                outfile = f.name
            
            cmd = [
                'dialog',
                '--title', title,
                '--inputbox', text,
                str(height), str(width)
            ]
            if init_text:
                cmd.append(init_text)
            
            result = subprocess.run(cmd, stdout=open(outfile, 'w'), 
                                   stderr=subprocess.DEVNULL)
            
            if result.returncode == 0:
                with open(outfile, 'r') as f:
                    value = f.read().strip()
                os.unlink(outfile)
                return value
            
            os.unlink(outfile)
            return None
        except Exception as e:
            Logger.error(f"Dialog error: {str(e)}")
            return None
    
    @staticmethod
    def passwordbox(title: str, text: str, height: int = 10, width: int = 70) -> Optional[str]:
        """Show a password input dialog"""
        try:
            with tempfile.NamedTemporaryFile(mode='r+', delete=False, suffix='.txt') as f:
                outfile = f.name
            
            result = subprocess.run([
                'dialog',
                '--title', title,
                '--insecure',
                '--passwordbox', text,
                str(height), str(width)
            ], stdout=open(outfile, 'w'), stderr=subprocess.DEVNULL)
            
            if result.returncode == 0:
                with open(outfile, 'r') as f:
                    value = f.read().strip()
                os.unlink(outfile)
                return value
            
            os.unlink(outfile)
            return None
        except Exception as e:
            Logger.error(f"Dialog error: {str(e)}")
            return None


class Configuration:
    """Global installation configuration"""

    def __init__(self):
        # OS configuration
        self.os_edition = None  # debian, freebsd, or arch

        # Boot configuration
        self.boot_mode = None  # UEFI or BIOS
        
        # Disk configuration
        self.disk = None
        self.part1 = None  # BIOS boot / EFI partition
        self.part2 = None  # EFI partition (UEFI) or boot partition
        self.part3 = None  # Boot pool partition
        self.part4 = None  # Root pool partition
        
        # ZFS configuration
        self.pool_name = "rpool"
        self.debian_mirror = "http://deb.debian.org/debian"
        
        # System configuration
        self.hostname = "nas"
        self.domain = "local"
        
        # User configuration
        self.username = "freedomnas_admin"
        self.user_fullname = "Admin User"
        self.user_password = None
        self.user_password_hash = None
        
        # Additional users
        self.additional_users = []


def check_root():
    """Check if running as root"""
    if os.geteuid() != 0:
        Logger.error("This installer must be run as root")
        sys.exit(1)


def run_command_safe(args: List[str], check: bool = True) -> Tuple[str, str]:
    """
    Run a command safely without shell injection.
    
    Preferred method for new code. Uses list-based subprocess execution.
    
    Args:
        args: Command as list of strings (e.g., ["zfs", "list", "-H"])
        check: If True, raise exception on non-zero return code
    
    Returns:
        Tuple of (stdout, stderr) as strings
    """
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=check,
            timeout=30
        )
        return result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        Logger.error(f"Command timed out: {' '.join(args)}")
        raise
    except subprocess.CalledProcessError as e:
        Logger.error(f"Command failed: {' '.join(args)}")
        Logger.error(f"Error: {e.stderr}")
        raise


def run_command(cmd: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """
    Run a shell command.
    
    DEPRECATED: Use run_command_safe() for new code.
    This function still uses shell=True for backward compatibility.
    
    Args:
        cmd: Command string to execute
        check: If True, raise exception on non-zero return code
        capture: If True, capture output
    
    Returns:
        CompletedProcess object
    """
    engine = EngineManager.get_engine()
    
    # Try to use engine if available for known commands
    # Otherwise fall back to subprocess for backward compatibility
    try:
        if capture:
            result = subprocess.run(cmd, shell=True, capture_output=True, 
                                   text=True, check=check, timeout=30)
        else:
            result = subprocess.run(cmd, shell=True, check=check, timeout=30)
        return result
    except subprocess.TimeoutExpired:
        Logger.error(f"Command timed out: {cmd}")
        raise
    except subprocess.CalledProcessError as e:
        Logger.error(f"Command failed: {cmd}")
        if hasattr(e, 'stderr') and e.stderr:
            Logger.error(f"Error: {e.stderr}")
        raise


def run_command_chroot(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    """
    Run a command inside chroot at /mnt.
    
    Args:
        cmd: Command to execute inside chroot
        check: If True, raise exception on non-zero return code
    
    Returns:
        CompletedProcess object
    """
    full_cmd = f'chroot /mnt /bin/bash -c "{cmd}"'
    return run_command(full_cmd, check=check)


def file_exists(path: str) -> bool:
    """Check if file exists"""
    return os.path.exists(path)


def directory_exists(path: str) -> bool:
    """Check if directory exists"""
    return os.path.isdir(path)


def read_file(path: str) -> str:
    """Read file contents"""
    try:
        with open(path, 'r') as f:
            return f.read()
    except Exception as e:
        Logger.warn(f"Could not read file {path}: {str(e)}")
        return ""


def write_file(path: str, content: str):
    """Write content to file"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(content)
    except Exception as e:
        Logger.error(f"Could not write to {path}: {str(e)}")
        raise


def get_command_output(cmd: str) -> str:
    """Get output of a command"""
    try:
        result = run_command(cmd, capture=True)
        return result.stdout.strip()
    except Exception:
        return ""


# =============================================================================
# FreedomNAS Core Engine Helper Functions
# =============================================================================

def engine_execute(category: str, action: str, **kwargs) -> Tuple[str, str]:
    """
    Execute a command through the FreedomNAS engine if available.
    
    This provides OS-agnostic command execution for operations like:
    - users: create, delete, set_password, list
    - services: start, stop, restart, enable, disable
    - zfs: pool_create, dataset_create, snapshot_create
    - network: set_static_ip, hostname_set
    
    Args:
        category: Command category (e.g., "services", "users", "zfs")
        action: Action within the category (e.g., "restart", "create", "list")
        **kwargs: Parameters for the command (e.g., service_name="samba", username="admin")
    
    Returns:
        Tuple of (stdout, stderr)
    
    Raises:
        Exception: If engine not available or command fails
    """
    engine = EngineManager.get_engine()
    
    if engine is None:
        raise RuntimeError("FreedomNAS Engine not available. Install core module or use run_command().")
    
    try:
        return engine.execute(category, action, **kwargs)
    except (CommandTranslationError, CommandExecutionError) as e:
        Logger.error(f"Engine command failed: {category}.{action}")
        Logger.error(f"Error: {e}")
        raise


def engine_get_os_info() -> dict:
    """Get OS information from the engine"""
    engine = EngineManager.get_engine()
    
    if engine is None:
        return {"error": "Engine not available"}
    
    return engine.get_os_info()


# Convenience functions for common operations using the engine

def create_user(username: str, groups: str = "samba") -> bool:
    """
    Create a system user using the engine.
    
    Args:
        username: Username to create
        groups: Comma-separated list of groups to add user to
    
    Returns:
        True on success, False on failure
    """
    try:
        engine_execute("users", "create", username=username, groups=groups)
        Logger.info(f"User {username} created successfully")
        return True
    except Exception as e:
        Logger.error(f"Failed to create user {username}: {e}")
        return False


def set_user_password(username: str, password: str) -> bool:
    """
    Set a user password using the engine.
    
    Args:
        username: Username
        password: New password
    
    Returns:
        True on success, False on failure
    """
    try:
        engine_execute("users", "set_password", username=username, password=password)
        Logger.info(f"Password for {username} set successfully")
        return True
    except Exception as e:
        Logger.error(f"Failed to set password for {username}: {e}")
        return False


def start_service(service_name: str) -> bool:
    """Start a service using the engine"""
    try:
        engine_execute("services", "start", service_name=service_name)
        Logger.info(f"Service {service_name} started")
        return True
    except Exception as e:
        Logger.error(f"Failed to start service {service_name}: {e}")
        return False


def stop_service(service_name: str) -> bool:
    """Stop a service using the engine"""
    try:
        engine_execute("services", "stop", service_name=service_name)
        Logger.info(f"Service {service_name} stopped")
        return True
    except Exception as e:
        Logger.error(f"Failed to stop service {service_name}: {e}")
        return False


def enable_service(service_name: str) -> bool:
    """Enable a service to start at boot using the engine"""
    try:
        engine_execute("services", "enable", service_name=service_name)
        Logger.info(f"Service {service_name} enabled")
        return True
    except Exception as e:
        Logger.error(f"Failed to enable service {service_name}: {e}")
        return False


def set_hostname_via_engine(hostname: str) -> bool:
    """Set system hostname using the engine"""
    try:
        engine_execute("system", "hostname_set", hostname=hostname)
        Logger.info(f"Hostname set to {hostname}")
        return True
    except Exception as e:
        Logger.error(f"Failed to set hostname: {e}")
        return False


def list_zfs_pools() -> str:
    """List ZFS pools using the engine"""
    try:
        stdout, stderr = engine_execute("zfs", "pool_list")
        return stdout
    except Exception as e:
        Logger.warn(f"Failed to list ZFS pools: {e}")
        return ""
