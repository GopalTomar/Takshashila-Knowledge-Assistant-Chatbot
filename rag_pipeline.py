from typing import List, Dict, Any
from vector_store import VectorStore
from llm_handler import LLMHandler
from config import Config

class RAGPipeline:
    def __init__(self):
        self.config = Config()
        self.vector_store = VectorStore()
        self.llm_handler = LLMHandler()
    
    def create_context_prompt(self, query: str, context_docs: List[Dict[str, Any]]) -> str:
        """Create a prompt with context for the LLM"""
        context_text = "\n\n".join([
            f"Document {i+1}: {doc['content']}" 
            for i, doc in enumerate(context_docs)
        ])
        
        prompt = f"""<bos><start_of_turn>user
Based on the following context documents, please answer the question. If the answer cannot be found in the context, please say so.

Context:
{context_text}

Question: {query}

Please provide a comprehensive answer based on the context provided.<end_of_turn>
<start_of_turn>model
"""
        
        return prompt
    
    def query(self, user_query: str) -> Dict[str, Any]:
        """Process a user query through the RAG pipeline"""
        # Step 1: Retrieve relevant documents
        relevant_docs = self.vector_store.similarity_search(
            user_query, 
            k=self.config.TOP_K_RESULTS
        )
        
        if not relevant_docs:
            return {
                "answer": "I don't have any relevant information in my knowledge base to answer your question.",
                "sources": [],
                "context_used": []
            }
        
        # Step 2: Create prompt with context
        prompt = self.create_context_prompt(user_query, relevant_docs)
        
        # Step 3: Generate response
        answer = self.llm_handler.generate_response(prompt)
        
        # Step 4: Prepare response with sources
        sources = []
        for doc in relevant_docs:
            source_info = {
                "file": doc['metadata'].get('source', 'Unknown'),
                "chunk_id": doc['metadata'].get('chunk_id', 0),
                "score": doc['score']
            }
            if source_info not in sources:
                sources.append(source_info)
        
        return {
            "answer": answer,
            "sources": sources[:3],  # Top 3 sources
            "context_used": [doc['content'][:200] + "..." for doc in relevant_docs[:2]]
        }