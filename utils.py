import os
import hashlib
import json
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path
import shutil

def setup_logging(log_level: str = "INFO") -> None:
    """Setup logging configuration"""
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('chatbot.log'),
            logging.StreamHandler()
        ]
    )

def get_file_hash(file_path: str) -> str:
    """Generate MD5 hash of a file for duplicate detection"""
    hash_md5 = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except Exception as e:
        logging.error(f"Error generating hash for {file_path}: {e}")
        return ""

def validate_file_type(file_path: str, supported_extensions: List[str] = None) -> bool:
    """Validate if file type is supported"""
    if supported_extensions is None:
        supported_extensions = ['.pdf', '.docx', '.txt', '.png', '.jpg', '.jpeg', '.xlsx', '.xls', '.bmp', '.tiff']
    
    file_extension = os.path.splitext(file_path)[1].lower()
    return file_extension in supported_extensions

def get_file_size_mb(file_path: str) -> float:
    """Get file size in MB"""
    try:
        size_bytes = os.path.getsize(file_path)
        return size_bytes / (1024 * 1024)
    except Exception as e:
        logging.error(f"Error getting file size for {file_path}: {e}")
        return 0.0

def clean_text(text: str) -> str:
    """Clean and normalize extracted text"""
    if not text:
        return ""
    
    # Remove excessive whitespace
    text = ' '.join(text.split())
    
    # Remove control characters but keep newlines
    cleaned_text = ''.join(char for char in text if ord(char) >= 32 or char == '\n')
    
    return cleaned_text.strip()

def truncate_text(text: str, max_length: int = 200) -> str:
    """Truncate text to specified length with ellipsis"""
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."

def format_file_size(size_bytes: int) -> str:
    """Format file size in human readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"

def create_directories(paths: List[str]) -> None:
    """Create multiple directories if they don't exist"""
    for path in paths:
        os.makedirs(path, exist_ok=True)

def save_metadata(metadata: Dict[str, Any], file_path: str) -> None:
    """Save metadata to JSON file"""
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False, default=str)
    except Exception as e:
        logging.error(f"Error saving metadata to {file_path}: {e}")

def load_metadata(file_path: str) -> Dict[str, Any]:
    """Load metadata from JSON file"""
    try:
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}
    except Exception as e:
        logging.error(f"Error loading metadata from {file_path}: {e}")
        return {}

def get_document_stats(documents_dir: str) -> Dict[str, Any]:
    """Get statistics about uploaded documents"""
    stats = {
        'total_files': 0,
        'total_size_mb': 0.0,
        'file_types': {},
        'upload_dates': []
    }
    
    try:
        if not os.path.exists(documents_dir):
            return stats
        
        for file_path in Path(documents_dir).rglob('*'):
            if file_path.is_file():
                stats['total_files'] += 1
                
                # File size
                size_mb = get_file_size_mb(str(file_path))
                stats['total_size_mb'] += size_mb
                
                # File type
                file_ext = file_path.suffix.lower()
                if file_ext:
                    stats['file_types'][file_ext] = stats['file_types'].get(file_ext, 0) + 1
                
                # Upload date (file creation time)
                creation_time = os.path.getctime(str(file_path))
                upload_date = datetime.fromtimestamp(creation_time).strftime('%Y-%m-%d')
                if upload_date not in stats['upload_dates']:
                    stats['upload_dates'].append(upload_date)
        
        # Sort upload dates
        stats['upload_dates'].sort(reverse=True)
        
    except Exception as e:
        logging.error(f"Error getting document stats: {e}")
    
    return stats

def sanitize_filename(filename: str) -> str:
    """Sanitize filename for safe storage"""
    # Remove or replace unsafe characters
    unsafe_chars = '<>:"/\\|?*'
    for char in unsafe_chars:
        filename = filename.replace(char, '_')
    
    # Limit length
    if len(filename) > 255:
        name, ext = os.path.splitext(filename)
        filename = name[:255-len(ext)] + ext
    
    return filename

def check_disk_space(path: str, required_mb: float = 100) -> bool:
    """Check if there's enough disk space"""
    try:
        statvfs = os.statvfs(path)
        free_mb = (statvfs.f_frsize * statvfs.f_bavail) / (1024 * 1024)
        return free_mb >= required_mb
    except Exception as e:
        logging.error(f"Error checking disk space: {e}")
        return True  # Assume OK if can't check

def backup_vector_db(source_dir: str, backup_dir: str) -> bool:
    """Create backup of vector database"""
    try:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = os.path.join(backup_dir, f'vectordb_backup_{timestamp}')
        
        if os.path.exists(source_dir):
            shutil.copytree(source_dir, backup_path)
            logging.info(f"Vector DB backed up to: {backup_path}")
            return True
        return False
    except Exception as e:
        logging.error(f"Error backing up vector DB: {e}")
        return False

def restore_vector_db(backup_path: str, target_dir: str) -> bool:
    """Restore vector database from backup"""
    try:
        if os.path.exists(backup_path) and os.path.exists(target_dir):
            shutil.rmtree(target_dir)
        
        shutil.copytree(backup_path, target_dir)
        logging.info(f"Vector DB restored from: {backup_path}")
        return True
    except Exception as e:
        logging.error(f"Error restoring vector DB: {e}")
        return False

def get_system_info() -> Dict[str, Any]:
    """Get system information for diagnostics"""
    import platform
    import psutil
    
    try:
        info = {
            'platform': platform.platform(),
            'python_version': platform.python_version(),
            'cpu_cores': psutil.cpu_count(),
            'memory_total_gb': round(psutil.virtual_memory().total / (1024**3), 2),
            'memory_available_gb': round(psutil.virtual_memory().available / (1024**3), 2),
            'disk_free_gb': round(shutil.disk_usage('.').free / (1024**3), 2)
        }
        return info
    except Exception as e:
        logging.error(f"Error getting system info: {e}")
        return {}

def estimate_processing_time(file_size_mb: float, file_type: str) -> int:
    """Estimate processing time in seconds based on file size and type"""
    # Base processing times per MB for different file types
    processing_times = {
        '.pdf': 2,      # seconds per MB
        '.docx': 1,
        '.txt': 0.5,
        '.xlsx': 3,
        '.xls': 3,
        '.png': 5,      # OCR is slower
        '.jpg': 5,
        '.jpeg': 5,
        '.bmp': 5,
        '.tiff': 5
    }
    
    base_time = processing_times.get(file_type.lower(), 2)  # default 2 sec/MB
    estimated_time = int(file_size_mb * base_time)
    
    return max(estimated_time, 1)  # minimum 1 second

def format_duration(seconds: int) -> str:
    """Format duration in human readable format"""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        remaining_seconds = seconds % 60
        return f"{minutes}m {remaining_seconds}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"

class DocumentMetadata:
    """Class to handle document metadata operations"""
    
    def __init__(self, metadata_dir: str):
        self.metadata_dir = metadata_dir
        os.makedirs(metadata_dir, exist_ok=True)
    
    def save_document_metadata(self, file_path: str, metadata: Dict[str, Any]) -> None:
        """Save metadata for a specific document"""
        file_hash = get_file_hash(file_path)
        metadata_file = os.path.join(self.metadata_dir, f"{file_hash}.json")
        
        # Add standard metadata
        metadata.update({
            'file_path': file_path,
            'file_name': os.path.basename(file_path),
            'file_size_mb': get_file_size_mb(file_path),
            'file_hash': file_hash,
            'upload_timestamp': datetime.now().isoformat(),
            'file_extension': os.path.splitext(file_path)[1].lower()
        })
        
        save_metadata(metadata, metadata_file)
    
    def get_document_metadata(self, file_path: str) -> Dict[str, Any]:
        """Get metadata for a specific document"""
        file_hash = get_file_hash(file_path)
        metadata_file = os.path.join(self.metadata_dir, f"{file_hash}.json")
        return load_metadata(metadata_file)
    
    def list_all_metadata(self) -> List[Dict[str, Any]]:
        """List metadata for all documents"""
        all_metadata = []
        try:
            for metadata_file in Path(self.metadata_dir).glob("*.json"):
                metadata = load_metadata(str(metadata_file))
                if metadata:
                    all_metadata.append(metadata)
        except Exception as e:
            logging.error(f"Error listing metadata: {e}")
        
        return all_metadata

def validate_model_file(model_path: str) -> Dict[str, Any]:
    """Validate LLM model file"""
    validation_result = {
        'valid': False,
        'exists': False,
        'size_mb': 0,
        'readable': False,
        'format': 'unknown'
    }
    
    try:
        # Check if file exists
        if os.path.exists(model_path):
            validation_result['exists'] = True
            
            # Check file size
            size_mb = get_file_size_mb(model_path)
            validation_result['size_mb'] = size_mb
            
            # Check if readable
            try:
                with open(model_path, 'rb') as f:
                    header = f.read(8)
                    validation_result['readable'] = True
                    
                    # Check GGUF format (starts with 'GGUF')
                    if header[:4] == b'GGUF':
                        validation_result['format'] = 'gguf'
                        validation_result['valid'] = True
                    
            except Exception as e:
                logging.error(f"Error reading model file: {e}")
        
    except Exception as e:
        logging.error(f"Error validating model file: {e}")
    
    return validation_result