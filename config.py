import os

class Config:
    # Paths
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    MODELS_DIR = os.path.join(BASE_DIR, "models")
    DOCUMENTS_DIR = os.path.join(BASE_DIR, "documents")
    VECTORDB_DIR = os.path.join(BASE_DIR, "vectordb")
    
    # Model configurations
    LLM_MODEL_PATH = os.path.join(MODELS_DIR, "gemma-3-1b-it.Q8_0.gguf")
    EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
    
    # Chunking parameters
    CHUNK_SIZE = 1000
    CHUNK_OVERLAP = 200
    
    # LLM parameters
    MAX_TOKENS = 512
    TEMPERATURE = 0.7
    
    # Retrieval parameters
    TOP_K_RESULTS = 5
    
    # Create directories if they don't exist
    os.makedirs(DOCUMENTS_DIR, exist_ok=True)
    os.makedirs(VECTORDB_DIR, exist_ok=True)
    os.makedirs(MODELS_DIR, exist_ok=True)