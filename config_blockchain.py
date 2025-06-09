# Configurações para APIs de Blockchain
# IMPORTANTE: Substitua pelas suas chaves de API para melhor funcionalidade

# Etherscan API (Ethereum) - Obtém em: https://etherscan.io/apis
# Gratuita: 5 req/s, Paga: 10 req/s
ETHERSCAN_API_KEY = "YourEtherscanAPIKey"  # Substitua pela sua chave

# PolygonScan API (Polygon) - Obtém em: https://polygonscan.com/apis
# Gratuita: 5 req/s, Paga: 10 req/s
POLYGON_API_KEY = "YourPolygonAPIKey"  # Substitua pela sua chave

# Infura (Ethereum Web3)
INFURA_PROJECT_ID = "YourInfuraProjectId"  # Obtenha em: https://infura.io/

# Blockstream (Bitcoin - não precisa de chave)
BITCOIN_API_URL = "https://blockstream.info/api"

# Endereço da sua Smart Wallet
SMART_WALLET_ADDRESS = "0x61C56137bd83eeeC5d532a351b2D99bD7d426339"

# Configurações de verificação
VERIFICACAO_INTERVALO = 60  # segundos
TOLERANCIA_ETH = 0.0001  # tolerância para Ethereum
TOLERANCIA_BTC = 0.00000001  # tolerância para Bitcoin
TOLERANCIA_MATIC = 0.0001  # tolerância para Polygon

# Configurações de timeout para APIs
API_TIMEOUT = 10  # segundos

# Configurações de verificação automática
VERIFICACAO_AUTOMATICA_ATIVA = True
INTERVALO_VERIFICACAO = 60  # segundos

# Configurações de logs
LOG_VERIFICACOES = True
LOG_TRANSACOES = True

# Webhooks (opcional)
WEBHOOK_URL = "https://seu-site.com/webhook/blockchain"

# Instruções de configuração:
# 1. Para Ethereum: Acesse https://etherscan.io/apis e crie uma conta gratuita
# 2. Para Polygon: Acesse https://polygonscan.com/apis e crie uma conta gratuita
# 3. Substitua as chaves "YourEtherscanAPIKey" e "YourPolygonAPIKey" pelas suas chaves reais
# 4. O sistema funcionará mesmo sem as chaves, mas com limitações de taxa

# Funcionalidades disponíveis:
# ✅ Verificação manual de transações
# ✅ Verificação automática de pagamentos
# ✅ Suporte a Bitcoin, Ethereum e Polygon
# ✅ Tolerância para variações de preço
# ✅ Logs detalhados
# ✅ Notificações automáticas
# ✅ Interface amigável 