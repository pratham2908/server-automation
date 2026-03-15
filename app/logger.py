"""Custom colorful console logger for the application."""

import logging
import sys
from collections import deque
from threading import Lock

# ANSI Colors
COLORS = {
    "RESET": "\033[0m",
    "RED": "\033[31m",
    "GREEN": "\033[32m",
    "YELLOW": "\033[33m",
    "BLUE": "\033[34m",
    "MAGENTA": "\033[35m",
    "CYAN": "\033[36m",
    "BRIGHT_GREEN": "\033[92m",
}

# Add custom SUCCESS log level (between INFO and WARNING)
SUCCESS_LEVEL = 25
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")

# Global log buffer for the live logs endpoint
LOG_BUFFER = deque(maxlen=200)
LOG_LOCK = Lock()

class DequeHandler(logging.Handler):
    """Custom handler that stores logs in a global deque for retrieval."""
    def emit(self, record):
        try:
            msg = self.format(record)
            with LOG_LOCK:
                LOG_BUFFER.append(msg)
        except Exception:
            self.handleError(record)

def get_logs():
    """Retrieve all logs currently in the buffer."""
    with LOG_LOCK:
        return list(LOG_BUFFER)


class ColorFormatter(logging.Formatter):
    """Custom formatter adding colors based on log level or specific kwargs."""

    def format(self, record: logging.LogRecord) -> str:
        # Default colors based on level
        color = COLORS["RESET"]
        
        if record.levelno == logging.ERROR or record.levelno == logging.CRITICAL:
            color = COLORS["RED"]
        elif record.levelno == logging.WARNING:
            color = COLORS["YELLOW"]
        elif record.levelno == SUCCESS_LEVEL:
            color = COLORS["GREEN"]
        elif record.levelno == logging.INFO:
            # Check if a specific color was requested via the 'extra' dict
            if hasattr(record, "color") and record.color in COLORS:
                color = COLORS[record.color]
        
        # Format the actual message
        msg = super().format(record)
        
        # Wrap the whole message in color (including timestamp/level if present)
        return f"{color}{msg}{COLORS['RESET']}"


def get_logger(name: str) -> logging.Logger:
    """Get a configured colorful logger."""
    logger = logging.getLogger(name)
    
    # Only configure if it doesn't already have handlers to avoid duplicates
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        
        handler = logging.StreamHandler(sys.stdout)
        # Format: [name] message
        formatter = ColorFormatter("[%(name)s] %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        
        # Add deque handler for live logs
        deque_handler = DequeHandler()
        # Non-colored formatter for the UI (can be styled in HTML)
        deque_formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
        deque_handler.setFormatter(deque_formatter)
        logger.addHandler(deque_handler)
        
        # Prevent propagation to the root logger so messages don't print twice
        logger.propagate = False
        
    return logger

def setup_root_logging():
    """Attach DequeHandler to the root logger to capture all logs."""
    root_logger = logging.getLogger()
    
    # Check if already added
    if not any(isinstance(h, DequeHandler) for h in root_logger.handlers):
        deque_handler = DequeHandler()
        deque_formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
        deque_handler.setFormatter(deque_formatter)
        root_logger.addHandler(deque_handler)

# Patch the Logger class to add a .success() method
def success(self, message, *args, **kws):
    if self.isEnabledFor(SUCCESS_LEVEL):
        self._log(SUCCESS_LEVEL, message, args, **kws)

logging.Logger.success = success  # type: ignore
