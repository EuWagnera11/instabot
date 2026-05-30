"""Wrapper de inicialização para diagnosticar erros no servidor."""
import sys
import traceback

try:
    import app
except Exception as e:
    # Se o app falhar, inicia um servidor mínimo que mostra o erro
    from flask import Flask
    error_app = Flask(__name__)
    error_msg = traceback.format_exc()
    
    @error_app.route("/health")
    def health():
        return {"status": "error", "message": error_msg}, 500
    
    @error_app.route("/")
    def index():
        return f"<pre>ERRO AO INICIAR:\n\n{error_msg}</pre>", 500
    
    print(f"ERRO: {e}", file=sys.stderr)
    print(error_msg, file=sys.stderr)
    error_app.run(host="0.0.0.0", port=5000)
