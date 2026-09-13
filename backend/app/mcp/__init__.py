"""Servidor MCP embutido no core (``/api/mcp``).

Ver ``gateway.py`` para o desenho. A regra que governa este pacote: o MCP é
uma segunda interface para o MESMO contrato do REST — cada ferramenta chama
os routers por loopback in-process, autenticada como o analista dono da chave.
"""
