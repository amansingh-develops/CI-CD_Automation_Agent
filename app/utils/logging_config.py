import logging
import sys
import os
from datetime import datetime

class ColoredFormatter(logging.Formatter):
    """Custom formatter to add colors to console output."""
    
    blue = "\x1b[38;5;39m"
    cyan = "\x1b[36m"
    green = "\x1b[32m"
    yellow = "\x1b[33m"
    red = "\x1b[31m"
    bold_red = "\x1b[31;1m"
    reset = "\x1b[0m"
    
    format_str = "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d - %(message)s"

    FORMATS = {
        logging.DEBUG: cyan + format_str + reset,
        logging.INFO: green + format_str + reset,
        logging.WARNING: yellow + format_str + reset,
        logging.ERROR: red + format_str + reset,
        logging.CRITICAL: bold_red + format_str + reset
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        # Handle cases where level might be outside standard range
        if not log_fmt:
            log_fmt = self.format_str
        formatter = logging.Formatter(log_fmt, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)

def setup_logging(level=logging.INFO):
    """Setup centralized logging configuration."""
    root_logger = logging.getLogger()
    
    # Clear existing handlers to prevent duplicate logs
    if root_logger.handlers:
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
            
    root_logger.setLevel(level)
    
    # 1. Console handler (using stderr for uvicorn compatibility)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(ColoredFormatter())
    root_logger.addHandler(console_handler)
    
    # 2. File handler for persistence
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    file_handler = logging.FileHandler(
        os.path.join(log_dir, f"agent_{datetime.now().strftime('%Y%m%d')}.log")
    )
    file_fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_fmt)
    root_logger.addHandler(file_handler)
    
    # Force propagation for all relevant internal loggers
    for logger_name in ["app", "uvicorn", "uvicorn.error", "uvicorn.access", "main"]:
        l = logging.getLogger(logger_name)
        l.setLevel(level)
        l.propagate = True
    
    root_logger.info("Logging initialized with enhanced format (Console + File).")
