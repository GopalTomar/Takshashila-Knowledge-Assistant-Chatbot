import os
import sys
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich import print as rprint
import shutil

from document_processor import DocumentProcessor
from rag_pipeline import RAGPipeline
from config import Config

class OfflineRAGChatbot:
    def __init__(self):
        self.console = Console()
        self.config = Config()
        self.document_processor = DocumentProcessor()
        self.rag_pipeline = None
        
        # Initialize RAG pipeline
        self.console.print("[yellow]Initializing RAG pipeline...[/yellow]")
        try:
            self.rag_pipeline = RAGPipeline()
            self.console.print("[green]✅ RAG pipeline initialized successfully![/green]")
        except Exception as e:
            self.console.print(f"[red]❌ Failed to initialize RAG pipeline: {e}[/red]")
            sys.exit(1)
    
    def display_welcome_message(self):
        """Display welcome message and instructions"""
        welcome_panel = Panel.fit(
            "[bold blue]🤖 Offline RAG Chatbot[/bold blue]\n"
            "[green]Privacy-focused, completely offline AI assistant[/green]\n\n"
            "[yellow]Available commands:[/yellow]\n"
            "• [cyan]upload[/cyan] - Upload and process documents\n"
            "• [cyan]status[/cyan] - Check knowledge base status\n"
            "• [cyan]clear[/cyan] - Clear knowledge base\n"
            "• [cyan]help[/cyan] - Show this help message\n"
            "• [cyan]quit[/cyan] - Exit the chatbot\n\n"
            "[dim]Just type your question to start chatting![/dim]",
            title="Welcome",
            border_style="blue"
        )
        self.console.print(welcome_panel)
    
    def upload_documents(self):
        """Handle document upload and processing"""
        self.console.print("\n[yellow]📁 Document Upload[/yellow]")
        self.console.print("Supported formats: PDF, DOCX, TXT, PNG, JPG, JPEG, XLSX, XLS")
        
        # Get file paths
        file_paths = []
        while True:
            file_path = Prompt.ask(
                "Enter file path (or 'done' to finish, 'cancel' to abort)"
            )
            
            if file_path.lower() == 'cancel':
                return
            
            if file_path.lower() == 'done':
                if file_paths:
                    break
                else:
                    self.console.print("[red]Please provide at least one file path[/red]")
                    continue
            
            if os.path.exists(file_path):
                file_paths.append(file_path)
                self.console.print(f"[green]✅ Added: {os.path.basename(file_path)}[/green]")
            else:
                self.console.print(f"[red]❌ File not found: {file_path}[/red]")
        
        if not file_paths:
            return
        
        # Process documents
        try:
            with self.console.status("[yellow]Processing documents...[/yellow]"):
                documents = self.document_processor.process_multiple_documents(file_paths)
            
            if documents:
                # Copy files to documents directory and add to vector store
                with self.console.status("[yellow]Adding to knowledge base...[/yellow]"):
                    for file_path in file_paths:
                        dest_path = os.path.join(
                            self.config.DOCUMENTS_DIR, 
                            os.path.basename(file_path)
                        )
                        shutil.copy2(file_path, dest_path)
                    
                    self.rag_pipeline.vector_store.add_documents(documents)
                
                self.console.print(f"[green]✅ Successfully processed {len(documents)} document chunks![/green]")
            else:
                self.console.print("[red]❌ No content extracted from the documents[/red]")
        
        except Exception as e:
            self.console.print(f"[red]❌ Error processing documents: {e}[/red]")
    
    def show_status(self):
        """Show knowledge base status"""
        count = self.rag_pipeline.vector_store.get_collection_count()
        
        table = Table(title="Knowledge Base Status")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="magenta")
        
        table.add_row("Document chunks", str(count))
        table.add_row("Storage location", self.config.VECTORDB_DIR)
        
        # List uploaded documents
        docs_dir = Path(self.config.DOCUMENTS_DIR)
        if docs_dir.exists():
            uploaded_files = list(docs_dir.iterdir())
            table.add_row("Uploaded files", str(len(uploaded_files)))
        
        self.console.print(table)
    
    def clear_knowledge_base(self):
        """Clear the knowledge base"""
        confirm = Prompt.ask(
            "Are you sure you want to clear the knowledge base? This cannot be undone",
            choices=["yes", "no"],
            default="no"
        )
        
        if confirm == "yes":
            self.rag_pipeline.vector_store.clear_collection()
            
            # Clear uploaded documents
            docs_dir = Path(self.config.DOCUMENTS_DIR)
            if docs_dir.exists():
                shutil.rmtree(docs_dir)
                os.makedirs(docs_dir)
            
            self.console.print("[green]✅ Knowledge base cleared![/green]")
    
    def process_query(self, query: str):
        """Process user query and display response"""
        with self.console.status("[yellow]Thinking...[/yellow]"):
            response = self.rag_pipeline.query(query)
        
        # Display answer
        answer_panel = Panel.fit(
            response["answer"],
            title="🤖 Answer",
            border_style="green"
        )
        self.console.print(answer_panel)
        
        # Display sources if available
        if response["sources"]:
            self.console.print("\n[dim]📚 Sources:[/dim]")
            for i, source in enumerate(response["sources"], 1):
                file_name = os.path.basename(source["file"])
                self.console.print(f"[dim]{i}. {file_name} (chunk {source['chunk_id']})[/dim]")
    
    def run(self):
        """Main chatbot loop"""
        self.display_welcome_message()
        
        while True:
            try:
                user_input = Prompt.ask("\n[bold cyan]You[/bold cyan]").strip()
                
                if not user_input:
                    continue
                
                # Handle commands
                if user_input.lower() == 'quit':
                    self.console.print("[yellow]👋 Goodbye![/yellow]")
                    break
                
                elif user_input.lower() == 'help':
                    self.display_welcome_message()
                
                elif user_input.lower() == 'upload':
                    self.upload_documents()
                
                elif user_input.lower() == 'status':
                    self.show_status()
                
                elif user_input.lower() == 'clear':
                    self.clear_knowledge_base()
                
                else:
                    # Process as query
                    if self.rag_pipeline.vector_store.get_collection_count() == 0:
                        self.console.print(
                            "[yellow]⚠️  No documents in knowledge base. "
                            "Use 'upload' command to add documents first.[/yellow]"
                        )
                    else:
                        self.process_query(user_input)
            
            except KeyboardInterrupt:
                self.console.print("\n[yellow]👋 Goodbye![/yellow]")
                break
            except Exception as e:
                self.console.print(f"[red]❌ Error: {e}[/red]")

if __name__ == "__main__":
    # Check if model file exists
    config = Config()
    if not os.path.exists(config.LLM_MODEL_PATH):
        print(f"❌ Model file not found: {config.LLM_MODEL_PATH}")
        print("Please ensure the gemma-3-1b-it.Q8_0.gguf file is placed in the models directory.")
        sys.exit(1)
    
    # Initialize and run chatbot
    chatbot = OfflineRAGChatbot()
    chatbot.run()