from llama_cpp import Llama
from config import Config
import logging

class LLMHandler:
    def __init__(self):
        self.config = Config()
        self.llm = None
        self.load_model()
    
    def load_model(self):
        """Load the local LLM model"""
        try:
            print("Loading local LLM model...")
            print(f"Model path: {self.config.LLM_MODEL_PATH}")
            
            self.llm = Llama(
                model_path=self.config.LLM_MODEL_PATH,
                n_ctx=2048,  # Context length
                n_threads=4,  # Number of threads
                verbose=False,
                n_gpu_layers=0  # Set to > 0 if you have GPU support
            )
            print("LLM model loaded successfully!")
            
        except Exception as e:
            logging.error(f"Error loading LLM model: {e}")
            raise e
    
    def generate_response(self, prompt: str, max_tokens: int = None) -> str:
        """Generate response using the local LLM"""
        try:
            if max_tokens is None:
                max_tokens = self.config.MAX_TOKENS
            
            response = self.llm(
                prompt,
                max_tokens=max_tokens,
                temperature=self.config.TEMPERATURE,
                top_p=0.9,
                repeat_penalty=1.1,
                stop=["</s>", "<|im_end|>"]
            )
            
            return response['choices'][0]['text'].strip()
            
        except Exception as e:
            logging.error(f"Error generating response: {e}")
            return "Sorry, I encountered an error generating a response."