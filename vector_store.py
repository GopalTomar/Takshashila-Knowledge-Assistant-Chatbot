import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer
from typing import List, Dict, Any
from langchain.schema import Document
import os
from config import Config

class VectorStore:
    def __init__(self):
        self.config = Config()
        
        # Initialize ChromaDB
        self.client = chromadb.PersistentClient(
            path=self.config.VECTORDB_DIR,
            settings=Settings(anonymized_telemetry=False)
        )
        
        # Initialize embedding model
        print("Loading embedding model...")
        self.embedding_model = SentenceTransformer(self.config.EMBEDDING_MODEL)
        print("Embedding model loaded successfully!")
        
        # Get or create collection
        self.collection = self.client.get_or_create_collection(
            name="documents",
            metadata={"hnsw:space": "cosine"}
        )
    
    def add_documents(self, documents: List[Document]) -> None:
        """Add documents to the vector store"""
        if not documents:
            return
        
        print(f"Adding {len(documents)} document chunks to vector store...")
        
        # Prepare data for ChromaDB
        texts = [doc.page_content for doc in documents]
        metadatas = [doc.metadata for doc in documents]
        ids = [f"doc_{i}" for i in range(len(documents))]
        
        # Generate embeddings
        print("Generating embeddings...")
        embeddings = self.embedding_model.encode(texts, show_progress_bar=True)
        
        # Add to collection
        self.collection.add(
            embeddings=embeddings.tolist(),
            documents=texts,
            metadatas=metadatas,
            ids=ids
        )
        
        print(f"Successfully added {len(documents)} document chunks!")
    
    def similarity_search(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        """Search for similar documents"""
        # Generate query embedding
        query_embedding = self.embedding_model.encode([query])
        
        # Search in ChromaDB
        results = self.collection.query(
            query_embeddings=query_embedding.tolist(),
            n_results=k
        )
        
        # Format results
        formatted_results = []
        for i in range(len(results['documents'][0])):
            formatted_results.append({
                'content': results['documents'][0][i],
                'metadata': results['metadatas'][0][i],
                'score': results['distances'][0][i]
            })
        
        return formatted_results
    
    def get_collection_count(self) -> int:
        """Get number of documents in collection"""
        return self.collection.count()
    
    def clear_collection(self) -> None:
        """Clear all documents from collection"""
        self.client.delete_collection("documents")
        self.collection = self.client.get_or_create_collection(
            name="documents",
            metadata={"hnsw:space": "cosine"}
        )
        print("Collection cleared!")