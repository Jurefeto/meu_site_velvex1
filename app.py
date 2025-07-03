import os
import re
import requests
import json
from datetime import datetime, timedelta
from functools import wraps
from markupsafe import escape, Markup
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from flask import (Flask, flash, redirect, render_template, request, session,
                   url_for, jsonify, send_file)

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from sqlalchemy.orm import joinedload # Importar joinedload

import mercadopago
from mercadopago.config import RequestOptions
from web3 import Web3
import time
from threading import Thread

# --- 1. CONFIGURAÇÃO DA APLICAÇÃO ---
app = Flask(__name__)
app.secret_key = 'sua_chave_secreta_velvex_super_segura_e_longa_aqui'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(BASE_DIR, 'velvex.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads', 'anuncios')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# --- 2. FILTROS JINJA2 ---
@app.template_filter('format_datetime')
def format_datetime_filter(value, format="%d/%m/%Y %H:%M"):
    if not value: return ""
    return value.strftime(format) if isinstance(value, datetime) else str(value)

@app.template_filter('nl2br')
def nl2br_filter(s):
    """Converte quebras de linha em tags <br> de forma segura."""
    if not s:
        return ""
    return Markup(escape(s).replace('\n', '<br>\n'))

# --- 3. MODELOS DO BANCO DE DADOS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    cpf = db.Column(db.String(14), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    endereco = db.Column(db.String(200), nullable=False)
    cep = db.Column(db.String(9), nullable=True)
    senha_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_banned = db.Column(db.Boolean, default=False, nullable=False)
    strike_count = db.Column(db.Integer, default=0, nullable=False)
    chat_banned_until = db.Column(db.DateTime, nullable=True)
    anuncios = db.relationship('Anuncio', backref='autor', lazy='select', cascade="all, delete-orphan")
    ofertas_feitas = db.relationship('Oferta', foreign_keys='Oferta.comprador_id', backref='comprador', lazy='select', cascade="all, delete-orphan")
    carrinho_itens = db.relationship('ItemCarrinho', backref='usuario', lazy=True, cascade="all, delete-orphan")
    notifications = db.relationship('Notification', backref='user', lazy=True, cascade="all, delete-orphan")
    sent_messages = db.relationship('Message', foreign_keys='Message.sender_id', backref='sender', lazy=True, cascade="all, delete-orphan")
    strike_logs = db.relationship('StrikeLog', backref='user', lazy=True, cascade="all, delete-orphan")
    favoritos = db.relationship('Favorito', backref='user', lazy=True, cascade="all, delete-orphan")
    pedidos = db.relationship('Pedido', backref='comprador', lazy='select', cascade="all, delete-orphan") # Renomeado para 'comprador'

class Anuncio(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(100), nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    categoria = db.Column(db.String(50), nullable=False)
    preco = db.Column(db.Float, nullable=False)
    condicao = db.Column(db.String(50), nullable=False)
    imagens_nomes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='pendente', nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    tamanho_numerico = db.Column(db.String(50), nullable=True)
    tamanho_roupa = db.Column(db.String(50), nullable=True)
    marca = db.Column(db.String(100), nullable=True)
    peso = db.Column(db.Float, default=0.5, nullable=False)
    altura = db.Column(db.Integer, default=10, nullable=False)
    largura = db.Column(db.Integer, default=10, nullable=False)
    comprimento = db.Column(db.Integer, default=10, nullable=False)
    ofertas = db.relationship('Oferta', backref='anuncio', lazy='dynamic', cascade="all, delete-orphan")
    carrinhos = db.relationship('ItemCarrinho', backref='anuncio', lazy=True, cascade="all, delete-orphan")
    notifications = db.relationship('Notification', backref='anuncio', lazy=True, cascade="all, delete-orphan")
    messages = db.relationship('Message', backref='anuncio', lazy=True, cascade="all, delete-orphan")
    strike_logs = db.relationship('StrikeLog', backref='anuncio', lazy=True, cascade="all, delete-orphan")
    favoritado_por = db.relationship('Favorito', backref='anuncio', lazy=True, cascade="all, delete-orphan")
    itens_pedido_associados = db.relationship('ItemPedido', backref='anuncio', lazy='dynamic', cascade="all, delete-orphan") # Nova relação

class Favorito(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    anuncio_id = db.Column(db.Integer, db.ForeignKey('anuncio.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('user_id', 'anuncio_id', name='_user_anuncio_uc'),)

class Oferta(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    anuncio_id = db.Column(db.Integer, db.ForeignKey('anuncio.id'), nullable=False)
    comprador_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    valor_oferta = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='pendente', nullable=False)
    data_oferta = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    notifications = db.relationship('Notification', backref='oferta', lazy=True, cascade="all, delete-orphan")

class ItemCarrinho(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    anuncio_id = db.Column(db.Integer, db.ForeignKey('anuncio.id'), nullable=False)
    quantidade = db.Column(db.Integer, default=1, nullable=False)
    frete_valor = db.Column(db.Float, nullable=True)
    frete_prazo = db.Column(db.Integer, nullable=True)
    frete_servico = db.Column(db.String(100), nullable=True)

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    message = db.Column(db.String(500), nullable=False)
    type = db.Column(db.String(50), nullable=True)
    anuncio_id = db.Column(db.Integer, db.ForeignKey('anuncio.id'), nullable=True)
    oferta_id = db.Column(db.Integer, db.ForeignKey('oferta.id'), nullable=True)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    anuncio_id = db.Column(db.Integer, db.ForeignKey('anuncio.id'), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False)

class StrikeLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    anuncio_id = db.Column(db.Integer, db.ForeignKey('anuncio.id'), nullable=False)
    message_content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

# Adicionar modelo para pedidos
class Pedido(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    total_brl = db.Column(db.Float, nullable=False)
    cripto_moeda = db.Column(db.String(50), nullable=True)
    cripto_valor = db.Column(db.Float, nullable=True)
    cripto_endereco = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(20), default='pendente')  # pendente, pago, cancelado
    data_criacao = db.Column(db.DateTime, default=datetime.utcnow)
    data_pagamento = db.Column(db.DateTime, nullable=True)
    hash_transacao = db.Column(db.String(100), nullable=True)
    
    # Ajustando o relacionamento para usar lazy='select'
    itens_pedido = db.relationship('ItemPedido', backref='pedido', lazy='select', cascade="all, delete-orphan")

# NOVO MODELO: ItemPedido
class ItemPedido(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido.id'), nullable=False)
    anuncio_id = db.Column(db.Integer, db.ForeignKey('anuncio.id'), nullable=False)
    quantidade = db.Column(db.Integer, nullable=False)
    preco_unitario = db.Column(db.Float, nullable=False) # Preço do item no momento da compra
    frete_servico = db.Column(db.String(100), nullable=True)
    frete_valor = db.Column(db.Float, nullable=True)
    frete_prazo = db.Column(db.Integer, nullable=True)


# --- 4. DECORADORES E PROCESSADORES DE CONTEXTO ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Por favor, faça login para acessar esta página.', 'warning')
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_admin'):
            flash('Acesso negado. Você precisa ser um administrador.', 'danger')
            return redirect(url_for('home'))
        return f(*args, **kwargs)
    return decorated_function

@app.context_processor
def inject_global_vars():
    context = {}
    if 'user_id' in session:
        user = User.query.get(session['user_id'])
        if user:
            context['user_is_banned_from_chat'] = user.chat_banned_until and user.chat_banned_until > datetime.utcnow()
        else:
            context['user_is_banned_from_chat'] = False
        context['unread_notifications_count'] = Notification.query.filter_by(user_id=session.get('user_id'), is_read=False).count()
    else:
        context['unread_notifications_count'] = 0
        context['user_is_banned_from_chat'] = False
    
    context['datetime'] = datetime
    context['current_user'] = User.query.get(session.get('user_id')) if session.get('user_id') else None
    context['is_logged_in'] = 'user_id' in session
    context['is_admin'] = session.get('is_admin', False)
    
    return context

def build_pagination_args(page_num):
    """Constrói os argumentos para a paginação mantendo os filtros atuais"""
    args = {}
    for key, value in request.args.items():
        if key != 'page':
            args[key] = value
    args['page'] = page_num
    return args

# --- 5. CONSTANTES GLOBAIS E DADOS DE FORMULÁRIO ---
INTERMEDIARY_CEP = "01001000"  # CEP do centro de distribuição
MELHOR_ENVIO_API_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJhdWQiOiIxIiwianRpIjoiODM1M2U5ZGNkMDhhYTE3NTRlOTQ4ZmE3NDZjNzFmZmQ2NjZjYWE1NWM2N2NkNTcwNzlmN2MzYzdjOWQ2ZDc4NTQ4MzNmNTg5MzFmZjhlNzAiLCJpYXQiOjE3NDk0OTg1NzAuMTI4ODU4LCJuYmYiOjE3NDk0OTg1NzAuMTI4ODU5LCJleHAiOjE3ODEwMzQ1NzAuMTE0NjA1LCJzdWIiOiI5ZjE5YTg0ZS1jNTY4LTQ3ZGYtYTM5Ny04NjEyZTk1YzdlOTAiLCJzY29wZXMiOlsiY2FydC13cml0ZSIsImNhcnQtcmVhZCIsInNoaXBwaW5nLWNhbGN1bGF0ZSIsInNoaXBwaW5nLWNhbmNlbCIsInNoaXBwaW5nLWNoZWNrb3V0Iiwic2hpcHBpbmctZ2VuZXJhdGUiLCJlY29tbWVyY2Utc2hpcHBpbmciLCJzaGlwcGluZy10cmFja2luZyJdfQ.R6QO4uwMXqQ8A7cCGcFnz20OtA8DpB97Bx0p6kkXBQHCKCMhTC7MdlxBFKjJBLt4EEFeVQfvUo6Ax2hayAz-PfsvTNdmuky4_tnkDjDKxeCa6Y8J8sOWtf0_W-sHLjj2vqiR84wQ9MUtFJ5MAS642dPLH_AnxmtTxgggfO6kSO_PPQGpsW7jwyxpKZK_MnMG_JUk-kSc6FE-ptEfP6kyHIJBdwUhcLTVPOxPz5q4SCTlVsYV8BNEu_iWUT6K3jJn3zt9zl1jgT2KMg3si41LjTrfzheQlJ0Qv9r7BuYXScffq39wvPdQQDJ69BLJQ6B7BiBDsm1XDe0p6a_HW6IwQb-EzoqgD-EAkEvcmUQXOkc8DMQ8osEvXmoXRjpJoW_TDHXll0F7oeGw_9KR0u4WKxN7kIYObRjlVCfH5cojMAq5iToZc79CzFmpxdAP1f2dQRLsoIQ3ZOHgowSF1b70E8oS6c3sAT7XMDWYzG5kwnNYKPS4dTWsoCXiGWe0_oNaDfetpKm_R47bFL_1GlCzUGIHqgPTkBq2RvmAoHNY5dIgbz2C51SYPhK-A2LTmZMGWZcj7YO4yjssHXsE0h5RPYw33CE31yWpPTPLaylGGVP1OWtxSJTS4q9yjkn8tu8sFnzOHIAiSGq3htfNlY1u1Myn1BxeWujWW-OVV4XLPOI"

import os
import requests
from flask import request, send_file, jsonify

MELHOR_ENVIO_API_URL_CALCULAR = "https://www.melhorenvio.com.br/api/v2/me/shipment/calculate"
MELHOR_ENVIO_API_URL = "https://www.melhorenvio.com.br/api/v2/shipment"

@app.route('/emitir-etiqueta', methods=['POST'])
def emitir_etiqueta():
    dados_envio = request.json  # Receba os dados do envio via JSON

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {MELHOR_ENVIO_API_TOKEN}",
        "User-Agent": "Velvex App (contato@velvex.com)"
    }

    # 1. Criar envio (enviar como lista)
    if not isinstance(dados_envio, list):
        dados_envio = [dados_envio]
    resp = requests.post(MELHOR_ENVIO_API_URL, headers=headers, json=dados_envio)
    if resp.status_code != 200:
        return jsonify({"erro": "Erro ao criar envio", "detalhe": resp.text}), 400

    envio = resp.json()[0]  # O retorno é uma lista
    shipment_id = envio['id']

    # 2. Pagar envio
    pay_url = f"{MELHOR_ENVIO_API_URL}/{shipment_id}/pay"
    resp_pay = requests.post(pay_url, headers=headers)
    if resp_pay.status_code != 200:
        return jsonify({"erro": "Erro ao pagar envio", "detalhe": resp_pay.text}), 400

    # 3. Baixar etiqueta
    print_url = f"{MELHOR_ENVIO_API_URL}/{shipment_id}/print"
    headers_pdf = headers.copy()
    headers_pdf["Accept"] = "application/pdf"
    resp_pdf = requests.get(print_url, headers=headers_pdf)
    if resp_pdf.status_code != 200:
        return jsonify({"erro": "Erro ao baixar etiqueta", "detalhe": resp_pdf.text}), 400

    # Salva o PDF temporariamente e envia para download
    file_path = f"etiqueta_{shipment_id}.pdf"
    with open(file_path, "wb") as f:
        f.write(resp_pdf.content)
    return send_file(file_path, as_attachment=True)

# Configuração do Mercado Pago
MERCADO_PAGO_ACCESS_TOKEN = "TEST-5821472335048864-060815-3ebd6b8105be6d4a5538a87e2116c7ba-1595074859"
MERCADO_PAGO_PUBLIC_KEY = "TEST-9ded371f-357f-4a4d-82de-f3f5db0da38b"
mp = mercadopago.SDK(MERCADO_PAGO_ACCESS_TOKEN)

# Configuração para criptomoedas
CRYPTO_API_URL = "https://api.coingecko.com/api/v3"

# Configuração para APIs de blockchain
ETHERSCAN_API_KEY = "YourEtherscanAPIKey"  # Substitua pela sua chave
BITCOIN_API_URL = "https://mempool.space/api"  # API compatível com Bech32
POLYGON_API_KEY = "YourPolygonAPIKey"  # Substitua pela sua chave

# Configuração Web3
w3 = Web3(Web3.HTTPProvider('https://mainnet.infura.io/v3/your-project-id'))  # Substitua pelo seu endpoint

# Endereço da sua Smart Wallet
SMART_WALLET_ADDRESS = "bc1qzm8e7u7auwjp7gqeetutknjradrtvzq9mgupd4"

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
OFERTAS_POR_PAGINA = 5

CATEGORIAS_FORM = [
    {'value': 'sneakers', 'label': 'Tênis (Sneakers)'}, {'value': 'vestuario_camisetas', 'label': 'Vestuário - Camisetas'},
    {'value': 'vestuario_moletons', 'label': 'Vestuário - Moletons'}, {'value': 'vestuario_jaquetas', 'label': 'Vestuário - Jaquetas'},
    {'value': 'vestuario_calcas_shorts', 'label': 'Vestuário - Calças & Shorts'}, {'value': 'acessorios_bones', 'label': 'Acessórios - Bonés & Gorros'},
    {'value': 'acessorios_bolsas', 'label': 'Acessórios - Bolsas & Mochilas'}, {'value': 'acessorios_outros', 'label': 'Acessórios - Outros'},
]
CONDICOES_FORM = [
    {'value': 'novo_com_etiqueta', 'label': 'Novo, com etiqueta'}, {'value': 'novo_sem_etiqueta', 'label': 'Novo, sem etiqueta'},
    {'value': 'usado_como_novo', 'label': 'Usado, como novo'}, {'value': 'usado_bom_estado', 'label': 'Usado, bom estado'},
    {'value': 'usado_com_marcas', 'label': 'Usado, com marcas de uso'},
]
CATEGORIAS_PAI_PARA_FILTER_STARTSWITH = {'vestuario': 'Vestuário', 'acessorios': 'Acessórios'}
MARCAS_PRODUTOS = [
    'Aape', 'Acne Studios', 'Adidas', 'Aged Archive', 'Aimé Leon Dore', 'Air Jordan', 'Alexander McQueen',
    'Alexander Wang', 'All Saints', 'Alo Yoga', 'Amiri', 'Anta Sports', 'Apple', 'Approve', 'AQVA', 'Arai',
    'Arc\'teryx', 'Arnette', 'Asics', 'ASSC', 'Balenciaga', 'Bally', 'Balmain', 'Bape', 'Barra Crew',
    'Baw Clothing', 'Bearbrick', 'Belmonth', 'Big Stuff', 'Billionaire Boys Club', 'Born x Raised',
    'Bottega Veneta', 'Brasil Artes Originais', 'Brazilian Apparel', 'Brunello Cucinelli', 'Brunx',
    'Bulova', 'Burberry', 'Cactus Plant Flea Market', 'Canon', 'Captive', 'Carhartt', 'Carnan', 'Cartier',
    'Casablanca', 'Casio', 'CBUM Store', 'Celine', 'Champion', 'Chloé', 'Christian Louboutin',
    'Chrome Hearts', 'Citizen', 'Class', 'Coach', 'COMME des GARÇONS', 'Converse', 'Corteiz',
    'Creed Fragrance', 'Crocs', 'Dainese', 'DC Shoes', 'Denim Tears', 'DGK', 'Diadora',
    'Diane Von Furstenberg', 'Diesel', 'Dime', 'Dior', 'DKNY', 'dp studio', 'Dr. Martens', 'Drew House',
    'Ducati', 'Dyson', 'Ed Hardy', 'Egho Studios', 'Eric Emanuel', 'Escada', 'Evisu', 'Evoke',
    'Exclusiviist 1of1', 'FAITH BY LUIS', 'Fauré Le Page', 'Fear of God Essentials', 'Fendi', 'Fila',
    'Fivebucks Company', 'Fossil', 'FUBU', 'Funko', 'Furla', 'Gallery Dept.', 'Garré', 'Ghostwrite',
    'Giorgio Armani', 'Giuseppe Zanotti', 'Givenchy', 'Godspeed New York', 'Golden Goose', 'Golf Wang',
    'Goyard', 'Gucci', 'Hard Rock Cafe', 'Harley Davidson', 'Havaianas', 'Hellstar', 'Hermès',
    'Heron Preston', 'High Company', 'Hoka', 'HotWheels', 'HUF', 'Hurt', 'Hypebeast', 'Impie',
    'Independent', 'Initio Parfums', 'Invicta', 'Iron Studios', 'Jacquemus', 'Janie and Jack',
    'Jimmy Choo', 'Jours de pluie', 'Judith Leiber', 'JW Anderson', 'Kace Wear', 'Karl Lagerfeld',
    'Karol G Store', 'Kate Spade New York', 'KAWS', 'Kenzo', 'Kith', 'LAARVEE', 'Lacoste', 'Lanvin',
    'Le Coq Sportif', 'Les Petits Joueurs', 'Levi\'s', 'Li-Ning', 'Loewe', 'Lolitta', 'Longchamp',
    'Loro Piana', 'Louis Vuitton', 'Low Sintra', 'M-Experiment', 'Mad Enlatados', 'Mahpa',
    'Maison Francis Kurkdjian', 'Maison Mihara Yasuhiro', 'Mansur Gavriel', 'Marcelo Burlon', 'Market',
    'Marni', 'Marshall', 'Massimo Dutti', 'McDonald\'s', 'Meta', 'Michael Kors', 'Microsoft', 'Missoni',
    'Mitchell & Ness', 'Miu Miu', 'Mizuno', 'MLB', 'Moncler', 'Montblanc', 'Mormaii', 'Moschino', 'MSCHF',
    'MSGM', 'Mulberry', 'Museum of Contemporary Art Chicago', 'Nascar', 'Nautica', 'NBA', 'New Balance',
    'NHL', 'Nicks', 'Nike', 'Nintendo', 'Nishane', 'NK Store', 'Nude Project', 'Oakley', 'Obey',
    'Off-White', 'Omega', 'On Running', 'Onitsuka Tiger', 'oNMe', 'Orient', 'ORIS', 'OVO', 'Pace',
    'Palace', 'Palla World', 'Palm Angels', 'Parfums De Marly', 'Parra', 'Patagonia', 'Patta',
    'Paula Cademartori', 'Phaidon', 'Philipp Plein', 'Piet', 'Planet Hollywood', 'PlayStation', 'PNDA',
    'Pokémon TCG', 'Polo Ralph Lauren', 'Pop Mart', 'Post Malone', 'Prada', 'Preux', 'Primitive',
    'Proenza Schoulder', 'Pucci', 'Puma', 'P_Andrade', 'Quadro Creations', 'Raf Simons', 'Razer', 'Reebok',
    'RELLO', 'Represent', 'Revenge', 'Rhode', 'Rhude', 'Rick Owens', 'Rider', 'Rimowa', 'Rip N Dip',
    'Rizzoli', 'Roberto Cavalli', 'Rothco', 'Salomon', 'Salvatore Ferragamo', 'Saucony', 'Seculus',
    'Seiko', 'Sergio Rossi', 'Sergio Tacchini', 'Shui', 'SKYLRK', 'Snow Clothing', 'Sony',
    'Sopro Company', 'Sp5der', 'Speedo', 'Sporty & Rich', 'Sprayground', 'Stanley', 'Starter',
    'Stella McCartney', 'Stone Island', 'Straye', 'Stüssy', 'Sufgang', 'Superplastic', 'Supreme',
    'Swatch', 'Syna World', 'Tag Heuer', 'Takashi Murakami', 'Take-Off', 'Taschen', 'Technos', 'Telfar',
    'The North Face', 'The Official Kangol', 'The Saint', 'thisisneverthat', 'Thrasher', 'Timberland',
    'Tissot', 'Tiziana Terenzi', 'Tod\'s', 'Tommy Jeans', 'Tory Burch', 'Trapstar', 'Travis Scott',
    'True Religion', 'TUPODE', 'Umbro', 'Under Armour', 'UNIQLO', 'UNRATED', 'Usuall', 'Valentino', 'Vans',
    'Veja Shoes', 'VESCARTES', 'Vetements', 'VIHE', 'Vilebrequin', 'Vlone', 'Xerjoff', 'Yeezy',
    'Yves Saint Laurent', 'Zadig & Voltaire', 'Zara', 'Zeferino'
]

# --- 6. FUNÇÕES AUXILIARES ---
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def contains_forbidden_info(text):
    text_lower = text.lower()
    sanitized_for_digits = re.sub(r'[\s\-.\(\)]', '', text)
    if re.search(r'\d{2,}', sanitized_for_digits): return True
    
    contact_keywords_with_numbers = ['zap', 'wpp', 'celular', 'pix', 'contato', 'liga', 'telefone', 'número', 'numero', 'ddd', 'cep']
    if any(re.search(r'\b' + keyword + r'\b', text_lower) for keyword in contact_keywords_with_numbers) and re.search(r'\d', text): return True

    if re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', text_lower): return True
    forbidden_keywords = ['whatsapp', 'email', 'e-mail', 'mail', 'insta', 'instagram', 'face', 'facebook', 'telegram', 'privado', 'particular', 'arroba', '@', 'gmail', 'hotmail', 'outlook', 'mercado livre', 'olx', 'enjoei']
    if any(keyword in text_lower for keyword in forbidden_keywords): return True
    
    offensive_words = ['bosta', 'merda', 'caralho', 'puta', 'puto', 'foda-se', 'desgraça', 'cuzão', 'arrombado', 'viado', 'idiota', 'imbecil', 'otário', 'burro', 'vagabundo', 'golpe', 'fraude', 'ladrão']
    if any(re.search(r'\b' + word + r'\b', text_lower) for keyword in offensive_words): return True
    
    return False

def calcular_frete_melhor_envio(cep_origem, cep_destino, anuncio):
    if not MELHOR_ENVIO_API_TOKEN or not MELHOR_ENVIO_API_TOKEN.startswith("ey"):
        print("Erro: Token do Melhor Envio parece inválido ou não foi configurado.")
        return []

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {MELHOR_ENVIO_API_TOKEN}",
        "User-Agent": "Velvex App (contato@velvex.com)"
    }
    
    cep_origem_limpo = re.sub(r'\D', '', cep_origem)
    cep_destino_limpo = re.sub(r'\D', '', cep_destino)

    payload = {
        "from": {"postal_code": cep_origem_limpo},
        "to": {"postal_code": cep_destino_limpo},
        "products": [{
            "id": str(anuncio.id),
            "width": anuncio.largura,
            "height": anuncio.altura,
            "length": anuncio.comprimento,
            "weight": anuncio.peso,
            "insurance_value": anuncio.preco,
            "quantity": 1
        }]
    }

    try:
        # Usar a URL de cálculo de frete
        response = requests.post(MELHOR_ENVIO_API_URL_CALCULAR, headers=headers, json=payload, timeout=10)
        response.raise_for_status() 
        data = response.json()
        
        # Filtra apenas os serviços desejados
        servicos_permitidos = ['SEDEX', '.Package', '.Com']
        opcoes_filtradas = []
        
        for opt in data:
            if 'error' not in opt and any(servico in opt.get('name', '') for servico in servicos_permitidos):
                opcoes_filtradas.append(opt)
                
        return opcoes_filtradas
        
    except requests.exceptions.RequestException as e:
        print(f"Erro na requisição para Melhor Envio: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Detalhes do erro: {e.response.text}")
        return []
    except json.JSONDecodeError:
        print("Erro ao decodificar a resposta JSON do Melhor Envio.")
        return []

def obter_cotacao_cripto(moeda="bitcoin"):
    """Obtém a cotação atual de uma criptomoeda em BRL"""
    try:
        url = f"{CRYPTO_API_URL}/simple/price"
        params = {
            "ids": moeda,
            "vs_currencies": "brl"
        }
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        return data[moeda]["brl"]
    except Exception as e:
        print(f"Erro ao obter cotação da {moeda}: {str(e)}")
        return None

def calcular_valor_cripto(valor_brl, moeda="bitcoin"):
    """Calcula o valor em criptomoeda baseado no valor em BRL"""
    cotacao = obter_cotacao_cripto(moeda)
    if cotacao:
        return valor_brl / cotacao
    return None

def obter_cotacoes_multiplas():
    """Obtém cotações de múltiplas criptomoedas (apenas Bitcoin)"""
    moedas = ["bitcoin"]
    try:
        url = f"{CRYPTO_API_URL}/simple/price"
        params = {
            "ids": ",".join(moedas),
            "vs_currencies": "brl"
        }
        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Erro ao obter cotações múltiplas: {str(e)}")
        return {}

# --- 7. ROTAS PRINCIPAIS E DE PÁGINAS ESTÁTICAS ---
@app.route('/')
def home():
    ultimos_anuncios = Anuncio.query.filter_by(status='aprovado').order_by(Anuncio.id.desc()).limit(4).all()
    return render_template('index.html', anuncios=ultimos_anuncios)

@app.route('/quem-somos')
def quem_somos_page(): return render_template('quem_somos.html')

@app.route('/compre')
def compre_page(): return render_template('compre.html')

@app.route('/venda')
def venda_page(): return render_template('venda.html')

@app.route('/suporte')
def suporte_page(): return render_template('suporte.html')

@app.route('/termos')
def termos_page(): return render_template('termos.html')

# --- 8. ROTAS DE AUTENTICAÇÃO E CONTA ---
@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if 'user_id' in session: return redirect(url_for('home'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        senha = request.form.get('senha', '').strip()
        user = User.query.filter_by(email=email).first()
        if user and not user.is_banned and check_password_hash(user.senha_hash, senha):
            session.permanent = True
            session['user_id'] = user.id
            session['user_nome'] = user.nome
            session['is_admin'] = user.is_admin
            flash(f'Login bem-sucedido! Olá, {user.nome}!', 'success')
            return redirect(url_for('admin_dashboard') if user.is_admin else url_for('minha_conta_page'))
        flash('Email, senha inválidos ou conta banida.', 'danger')
    return render_template('login.html')

# ROTA DE CRIAR CONTA CORRIGIDA
@app.route('/criar-conta', methods=['GET', 'POST'])
def criar_conta_page():
    if 'user_id' in session:
        return redirect(url_for('home'))
    if request.method == 'POST':
        # Usar um dicionário para os dados do formulário facilita o reenvio para o template
        form_data = {
            'nome': request.form.get('nome', '').strip(),
            'cpf': request.form.get('cpf', '').strip(),
            'email_criar': request.form.get('email_criar', '').strip(),
            'endereco': request.form.get('endereco', '').strip(),
            'cep': request.form.get('cep', '').strip(),
            'senha_criar': request.form.get('senha_criar', ''),
            'confirmar_senha': request.form.get('confirmar_senha', '')
        }
        
        erros = False
        
        if not (2 <= len(form_data['nome']) <= 100):
            flash('Nome deve ter entre 2 e 100 caracteres.', 'danger')
            erros = True
        
        # Validação de CPF
        cpf_limpo = re.sub(r'[.\-]', '', form_data['cpf'])
        if not re.match(r'^\d{11}$', cpf_limpo):
            flash('CPF inválido. Use 11 dígitos.', 'danger')
            erros = True
        
        if not re.match(r'[^@]+@[^@]+\.[^@]+', form_data['email_criar']):
            flash('Formato de email inválido.', 'danger')
            erros = True
        
        if not (5 <= len(form_data['endereco']) <= 200):
            flash('Endereço deve ter entre 5 e 200 caracteres.', 'danger')
            erros = True
            
        # Validação do CEP
        cep_limpo_para_validacao = re.sub(r'\D', '', form_data['cep'])
        if not re.match(r'^\d{8}$', cep_limpo_para_validacao):
            flash('CEP inválido. Use o formato 00000-000.', 'danger')
            erros = True
            
        if len(form_data['senha_criar']) < 6:
            flash('A senha deve ter no mínimo 6 caracteres.', 'danger')
            erros = True
        
        if form_data['senha_criar'] != form_data['confirmar_senha']:
            flash('As senhas não coincidem!', 'danger')
            erros = True
            
        if User.query.filter_by(email=form_data['email_criar']).first():
            flash('Email já cadastrado.', 'danger')
            erros = True
        
        if User.query.filter_by(cpf=cpf_limpo).first():
            flash('CPF já cadastrado.', 'danger')
            erros = True
            
        if erros:
            return render_template('criar_conta.html', **form_data, email_criar=form_data['email_criar']) # Passar email_criar de volta
        
        # Hash da senha aqui
        senha_hash = generate_password_hash(form_data['senha_criar'], method='pbkdf2:sha256')


        novo_usuario = User(
            nome=form_data['nome'],
            cpf=cpf_limpo,
            email=form_data['email_criar'],
            endereco=form_data['endereco'],
            cep=cep_limpo_para_validacao,
            senha_hash=senha_hash,
            is_admin=not bool(User.query.first())
        )
        db.session.add(novo_usuario)
        db.session.commit()
        
        flash(f'Conta para {novo_usuario.nome} criada com sucesso! Faça o login.', 'success')
        return redirect(url_for('login_page'))
        
    return render_template('criar_conta.html')


@app.route('/esqueceu-senha', methods=['GET', 'POST'])
def esqueceu_senha_page():
    flash('Funcionalidade de recuperação de senha ainda não implementada.', 'info')
    return redirect(url_for('login_page'))

@app.route('/logout')
def logout():
    session.clear()
    flash('Você foi desconectado.', 'info')
    return redirect(url_for('home'))

@app.route('/minha-conta', methods=['GET', 'POST'])
@login_required
def minha_conta_page():
    user = User.query.get(session['user_id'])
    
    if request.method == 'POST':
        # Atualizar informações do perfil
        user.nome = request.form.get('perfil_nome')
        user.endereco = request.form.get('perfil_endereco')
        user.cep = request.form.get('perfil_cep')
        
        # Alterar senha se fornecida
        senha_atual = request.form.get('perfil_senha_atual')
        nova_senha = request.form.get('perfil_nova_senha')
        confirmar_senha = request.form.get('perfil_confirmar_nova_senha')
        
        if senha_atual and nova_senha and confirmar_senha:
            if check_password_hash(user.senha_hash, senha_atual):
                if nova_senha == confirmar_senha:
                    user.senha_hash = generate_password_hash(nova_senha)
                    flash('Senha alterada com sucesso!', 'success')
                else:
                    flash('As senhas não coincidem.', 'danger')
            else:
                flash('Senha atual incorreta.', 'danger')
        
        db.session.commit()
        flash('Perfil atualizado com sucesso!', 'success')
        return redirect(url_for('minha_conta_page'))
    
    # Buscar favoritos com pesquisa
    favoritos_search_query = request.args.get('favoritos_search', '')
    meus_favoritos = Favorito.query.filter_by(user_id=user.id).join(Anuncio).filter(
        Anuncio.titulo.contains(favoritos_search_query) if favoritos_search_query else True
    ).all()
    
    # Buscar anúncios com pesquisa
    anuncios_search_query = request.args.get('anuncios_search', '')
    meus_anuncios = Anuncio.query.filter_by(user_id=user.id).filter(
        Anuncio.titulo.contains(anuncios_search_query) if anuncios_search_query else True
    ).all()
    
    # Buscar ofertas recebidas com paginação
    ofertas_r_page = request.args.get('ofertas_r_page', 1, type=int)
    ofertas_recebidas_paginadas = Oferta.query.join(Anuncio).filter(
        Anuncio.user_id == user.id
    ).order_by(Oferta.data_oferta.desc()).paginate(
        page=ofertas_r_page, per_page=5, error_out=False
    )
    
    # Buscar ofertas feitas com paginação
    ofertas_f_page = request.args.get('ofertas_f_page', 1, type=int)
    ofertas_feitas_paginadas = Oferta.query.filter_by(comprador_id=user.id).order_by(
        Oferta.data_oferta.desc()
    ).paginate(page=ofertas_f_page, per_page=5, error_out=False)
    
    # Minhas Vendas (agora filtrando corretamente os pedidos onde o usuário é o vendedor de algum item)
    minhas_vendas_pedidos = Pedido.query.join(ItemPedido).join(Anuncio).filter(
        Anuncio.user_id == user.id # Filtra pedidos que contêm anúncios do vendedor atual
    ).distinct().all()
    
    # Minhas Compras (já estava carregando pedidos do usuário como comprador)
    minhas_compras_pedidos = Pedido.query.filter_by(user_id=user.id).all()
    
    return render_template('minha_conta.html', 
                         user=user,
                         meus_favoritos=meus_favoritos,
                         meus_anuncios=meus_anuncios,
                         ofertas_recebidas_paginadas=ofertas_recebidas_paginadas,
                         ofertas_feitas_paginadas=ofertas_feitas_paginadas,
                         minhas_vendas=minhas_vendas_pedidos,
                         minhas_compras=minhas_compras_pedidos,
                         favoritos_search_query=favoritos_search_query,
                         anuncios_search_query=anuncios_search_query)

# --- 9. ROTAS DE ANÚNCIOS E LOJA ---
@app.route('/vender', methods=['GET', 'POST'])
@login_required
def loja_page():
    if request.method == 'POST':
        form_data = request.form
        uploaded_files = request.files.getlist("anuncio_imagens")
        erros = False
        try:
            preco = float(form_data.get('anuncio_preco'))
            if not (5 <= len(form_data.get('anuncio_titulo', '')) <= 100):
                flash('O título deve ter entre 5 e 100 caracteres.', 'danger'); erros = True
            if preco <= 0: flash('O preço deve ser maior que zero.', 'danger'); erros = True
        except (ValueError, TypeError):
            flash('Preço inválido ou não informado.', 'danger'); erros = True; preco = 0
        if not any(f.filename for f in uploaded_files):
            flash('É necessário enviar pelo menos uma imagem.', 'danger'); erros = True
        if erros:
            return render_template('loja.html', **form_data, categorias_form=CATEGORIAS_FORM, condicoes_form=CONDICOES_FORM, marcas_produtos=MARCAS_PRODUTOS)

        nomes_imagens_salvas = []
        for f in uploaded_files:
            if f and allowed_file(f.filename):
                nome_seguro = secure_filename(f.filename)
                f.save(os.path.join(app.config['UPLOAD_FOLDER'], nome_seguro))
                nomes_imagens_salvas.append(nome_seguro)

        novo_anuncio = Anuncio(
            titulo=form_data.get('anuncio_titulo'), descricao=form_data.get('anuncio_descricao'),
            categoria=form_data.get('anuncio_categoria'), preco=preco, condicao=form_data.get('anuncio_condicao'),
            user_id=session['user_id'], imagens_nomes=",".join(nomes_imagens_salvas),
            tamanho_numerico=form_data.get('tamanho_numerico') or None,
            tamanho_roupa=form_data.get('tamanho_roupa') or None,
            marca=request.form.get('anuncio_marca') or None
        )
        db.session.add(novo_anuncio)
        db.session.commit()
        flash(f'Anúncio "{novo_anuncio.titulo}" enviado para aprovação!', 'success')
        return redirect(url_for('minha_conta_page'))
    return render_template('loja.html', categorias_form=CATEGORIAS_FORM, condicoes_form=CONDICOES_FORM, marcas_produtos=MARCAS_PRODUTOS)

@app.route('/anuncios')
def anuncios_page():
    args = request.args
    page = request.args.get('page', 1, type=int)
    per_page = 12  # Número de anúncios por página
    
    query = Anuncio.query.filter_by(status='aprovado')
    
    # Aplicar filtros
    if args.get('query_busca'): 
        search_term = args.get('query_busca')
        query = query.filter(
            or_(
                Anuncio.titulo.ilike(f"%{search_term}%"),
                Anuncio.descricao.ilike(f"%{search_term}%"),
                Anuncio.marca.ilike(f"%{search_term}%")
            )
        )
    if args.get('categoria_filtro'):
        if args.get('categoria_filtro') in CATEGORIAS_PAI_PARA_FILTER_STARTSWITH:
            query = query.filter(Anuncio.categoria.startswith(args.get('categoria_filtro') + '_'))
        else:
            query = query.filter_by(categoria=args.get('categoria_filtro'))
    try:
        if args.get('preco_min'): 
            query = query.filter(Anuncio.preco >= float(args.get('preco_min')))
        if args.get('preco_max'): 
            query = query.filter(Anuncio.preco <= float(args.get('preco_max')))
    except ValueError: 
        flash("Valor de preço inválido.", "warning")
    if args.get('condicao_filtro'): 
        query = query.filter_by(condicao=args.get('condicao_filtro'))
    if args.get('tamanho_numerico_filtro'): 
        query = query.filter(Anuncio.tamanho_numerico.ilike(f"%{args.get('tamanho_numerico_filtro')}%"))
    if args.get('tamanho_roupa_filtro'): 
        query = query.filter(Anuncio.tamanho_roupa.ilike(f"%{args.get('tamanho_roupa_filtro')}%"))
    if args.get('marca_filtro'): 
        query = query.filter_by(marca=args.get('marca_filtro'))

    # Ordenação
    ordenar_por = args.get('ordenar_por', 'recentes')
    if ordenar_por == 'preco_asc':
        query = query.order_by(Anuncio.preco.asc())
    elif ordenar_por == 'preco_desc':
        query = query.order_by(Anuncio.preco.desc())
    else:  # 'recentes' (padrão)
        query = query.order_by(Anuncio.id.desc())
    
    # Aplicar paginação
    pagination = query.paginate(
        page=page, 
        per_page=per_page, 
        error_out=False
    )
    
    anuncios = pagination.items
    
    # Obter favoritos do usuário logado
    favoritos_ids = set()
    if 'user_id' in session:
        favoritos_ids = {f.anuncio_id for f in Favorito.query.filter_by(user_id=session['user_id']).all()}
    
    return render_template('anuncios.html',
                           anuncios=anuncios, 
                           pagination=pagination,
                           titulo_da_pagina="Nossos Anúncios",
                           filtros_atuais=args, 
                           categorias_form=CATEGORIAS_FORM,
                           condicoes_form=CONDICOES_FORM, 
                           CATEGORIAS_PAI_PARA_FILTER_STARTSWITH=CATEGORIAS_PAI_PARA_FILTER_STARTSWITH,
                           tamanhos_numericos_opcoes=list(range(34, 47)), 
                           tamanhos_roupa_opcoes=['P', 'M', 'G', 'GG', 'XG'],
                           favoritos_ids=favoritos_ids, 
                           marcas_produtos=MARCAS_PRODUTOS,
                           build_pagination_args=build_pagination_args)

@app.route('/anuncio/<int:anuncio_id>')
def detalhes_anuncio_page(anuncio_id):
    anuncio = Anuncio.query.get_or_404(anuncio_id)
    autor = User.query.get(anuncio.user_id)
    
    # Verifica se o autor existe e tem CEP
    if not autor or not autor.cep:
        flash('O vendedor não possui CEP cadastrado. Não será possível calcular o frete.', 'warning')
    
    # Adiciona o autor ao objeto anuncio
    anuncio.autor = autor
    
    # Busca outros anúncios do mesmo vendedor
    other_seller_ads = Anuncio.query.filter(
        Anuncio.user_id == anuncio.user_id,
        Anuncio.id != anuncio_id
    ).limit(4).all()
    
    # Busca ofertas recebidas para este anúncio
    ofertas = Oferta.query.filter_by(anuncio_id=anuncio_id).all()
    
    # Verifica se o anúncio está nos favoritos do usuário
    is_favorito = False
    if 'user_id' in session:
        favorito = Favorito.query.filter_by(
            user_id=session['user_id'],
            anuncio_id=anuncio_id
        ).first()
        is_favorito = favorito is not None
    
    # Verifica se o anúncio tem as dimensões necessárias
    if not all([anuncio.largura, anuncio.altura, anuncio.comprimento, anuncio.peso]):
        flash('O anúncio não possui todas as dimensões necessárias para cálculo do frete. Por favor, edite o anúncio e adicione as dimensões.', 'warning')

    return render_template('detalhes_anuncio.html',
                         anuncio=anuncio,
                         other_seller_ads=other_seller_ads,
                         ofertas=ofertas,
                         is_favorito=is_favorito)

@app.route('/anuncios-do-vendedor/<int:user_id>')
def anuncios_do_vendedor_page(user_id):
    seller = User.query.get_or_404(user_id)
    seller_ads = Anuncio.query.filter_by(user_id=user_id, status='aprovado').order_by(Anuncio.id.desc()).all()

    favoritos_ids = set()
    if 'user_id' in session:
        favoritos_ids = {f.anuncio_id for f in Favorito.query.filter_by(user_id=session['user_id']).all()}

    return render_template('mais_anuncios_vendedor.html',
                           seller=seller, anuncios=seller_ads, favoritos_ids=favoritos_ids)
                           
@app.route('/anuncio/<int:anuncio_id>/editar', methods=['GET', 'POST'])
@login_required
def editar_anuncio_action(anuncio_id):
    anuncio = Anuncio.query.get_or_404(anuncio_id)
    if anuncio.user_id != session.get('user_id') and not session.get('is_admin'):
        flash('Você não tem permissão para editar este anúncio.', 'danger')
        return redirect(url_for('home'))
        
    if request.method == 'POST':
        preco_antigo = anuncio.preco
        novo_preco = float(request.form.get('anuncio_preco'))
        
        anuncio.titulo = request.form.get('anuncio_titulo')
        anuncio.descricao = request.form.get('anuncio_descricao')
        anuncio.categoria = request.form.get('anuncio_categoria')
        anuncio.preco = novo_preco
        anuncio.condicao = request.form.get('anuncio_condicao')
        anuncio.marca = request.form.get('anuncio_marca') or None
        if not session.get('is_admin'): 
            anuncio.status = 'pendente'
        
        if novo_preco < preco_antigo:
            for favorito in anuncio.favoritado_por:
                if favorito.user_id != anuncio.user_id: # Only notify if not the seller
                    db.session.add(Notification(
                        user_id=favorito.user_id,
                        message=f'Alerta de Preço! O item "{anuncio.titulo}" que você favoritou baixou para R$ {novo_preco:.2f}.',
                        type='price_drop',
                        anuncio_id=anuncio.id
                    ))

        db.session.commit()
        flash('Anúncio atualizado com sucesso! Se necessário, ele passará por nova aprovação.', 'success')
        return redirect(url_for('minha_conta_page'))
        
    return render_template('editar_anuncio.html', anuncio=anuncio, categorias_form=CATEGORIAS_FORM, condicoes_form=CONDICOES_FORM, marcas_produtos=MARCAS_PRODUTOS)

@app.route('/anuncio/<int:anuncio_id>/deletar', methods=['POST'])
@login_required
def deletar_anuncio_action(anuncio_id):
    anuncio = Anuncio.query.get_or_404(anuncio_id)
    if anuncio.user_id == session.get('user_id') or session.get('is_admin'):
        db.session.delete(anuncio)
        db.session.commit()
        flash('Anúncio deletado com sucesso.', 'success')
    return redirect(request.referrer or url_for('home'))

# --- 10. ROTAS DE AÇÕES (FAVORITOS, CARRINHO, OFERTAS) ---
@app.route('/toggle-favorito/<int:anuncio_id>', methods=['POST'])
@login_required
def toggle_favorito(anuncio_id):
    anuncio = Anuncio.query.get_or_404(anuncio_id)
    favorito_existente = Favorito.query.filter_by(user_id=session['user_id'], anuncio_id=anuncio_id).first()
    if favorito_existente:
        db.session.delete(favorito_existente)
        flash(f'"{anuncio.titulo}" removido dos seus favoritos.', 'info')
    else:
        db.session.add(Favorito(user_id=session['user_id'], anuncio_id=anuncio_id))
        flash(f'"{anuncio.titulo}" adicionado aos seus favoritos!', 'success')
    db.session.commit()
    return redirect(request.referrer or url_for('home'))

@app.route('/carrinho/adicionar/<int:anuncio_id>', methods=['POST'])
@login_required
def adicionar_ao_carrinho_action(anuncio_id):
    anuncio = Anuncio.query.get_or_404(anuncio_id)
    if anuncio.user_id == session['user_id']:
        flash('Não pode comprar o seu próprio produto.', 'warning')
    elif anuncio.status != 'aprovado':
        flash('Este item não está mais disponível para venda.', 'warning')
    elif ItemCarrinho.query.filter_by(user_id=session['user_id'], anuncio_id=anuncio_id).first():
        flash('Este item já está no seu carrinho.', 'info')
    else:
        db.session.add(ItemCarrinho(user_id=session['user_id'], anuncio_id=anuncio_id))
        db.session.commit()
        flash(f'"{anuncio.titulo}" foi adicionado ao seu carrinho.', 'success')
        return redirect(url_for('carrinho_page'))
    return redirect(request.referrer or url_for('home'))

@app.route('/carrinho/remover-item/<int:item_id>', methods=['POST'])
@login_required
def remover_item_carrinho_action(item_id):
    item = ItemCarrinho.query.get_or_404(item_id)
    if item.user_id != session['user_id']:
        flash('Você não tem permissão para remover este item.', 'danger')
        return redirect(url_for('carrinho_page'))
    
    db.session.delete(item)
    db.session.commit()
    flash('Item removido do carrinho.', 'info')
    return redirect(url_for('carrinho_page'))

@app.route('/carrinho/limpar', methods=['POST'])
@login_required
def limpar_carrinho_action():
    itens_carrinho = ItemCarrinho.query.filter_by(user_id=session['user_id']).all()
    for item in itens_carrinho:
        db.session.delete(item)
    db.session.commit()
    flash('Carrinho limpo com sucesso.', 'info')
    return redirect(url_for('carrinho_page'))

@app.route('/carrinho/atualizar-quantidade/<int:item_id>', methods=['POST'])
@login_required
def atualizar_quantidade_carrinho_action(item_id):
    item = ItemCarrinho.query.get_or_404(item_id)
    if item.user_id != session['user_id']:
        return jsonify({"success": False, "error": "Você não tem permissão para alterar este item."}), 403
    
    quantidade = request.form.get('quantidade', type=int)
    if quantidade is None or quantidade < 1 or quantidade > 10:
        return jsonify({"success": False, "error": "Quantidade inválida. Deve ser entre 1 e 10."}), 400
    
    item.quantidade = quantidade
    db.session.commit()
    
    return jsonify({"success": True, "message": "Quantidade atualizada com sucesso."})

@app.route('/carrinho')
@login_required
def carrinho_page():
    itens_carrinho = ItemCarrinho.query.options(joinedload(ItemCarrinho.anuncio)).filter_by(user_id=session['user_id']).all()
    total_carrinho = sum(item.anuncio.preco * item.quantidade for item in itens_carrinho if item.anuncio)
    total_frete = sum(item.frete_valor or 0 for item in itens_carrinho)
    
    # Calcular taxas adicionais
    taxa_autenticidade = 20.00  # R$ 20,00 pela verificação de autenticidade
    # taxa_intermediacao removida
    
    total_geral = total_carrinho + total_frete + taxa_autenticidade
    todos_fretes_selecionados = all(item.frete_valor is not None for item in itens_carrinho)
    
    return render_template('carrinho.html', 
                         itens_carrinho=itens_carrinho, 
                         total_carrinho=total_carrinho, 
                         total_frete=total_frete, 
                         total_geral=total_geral,
                         taxa_autenticidade=taxa_autenticidade,
                         todos_fretes_selecionados=todos_fretes_selecionados)

@app.route('/checkout')
@login_required
def checkout_page():
    itens_carrinho = ItemCarrinho.query.options(joinedload(ItemCarrinho.anuncio)).filter_by(user_id=session['user_id']).all()
    if not itens_carrinho:
        flash('O seu carrinho está vazio.', 'warning')
        return redirect(url_for('carrinho_page'))
    
    total_carrinho = sum(item.anuncio.preco * item.quantidade for item in itens_carrinho if item.anuncio)
    total_frete = sum(item.frete_valor or 0 for item in itens_carrinho)
    
    # Calcular taxas adicionais
    taxa_autenticidade = 20.00  # R$ 20,00 pela verificação de autenticidade
    # taxa_intermediacao removida
    
    total_geral = total_carrinho + total_frete + taxa_autenticidade
    todos_fretes_selecionados = all(item.frete_valor is not None for item in itens_carrinho)
    
    # Calcular valores em múltiplas criptomoedas
    cotacoes = obter_cotacoes_multiplas()
    valores_cripto = {}
    for moeda in cotacoes:
        if cotacoes[moeda].get("brl"):
            valores_cripto[moeda] = total_geral / cotacoes[moeda]["brl"]
    
    # Gera o ID de preferência do Mercado Pago
    try:
        print("Iniciando criação da preferência de pagamento...")
        
        # Primeiro, vamos verificar a conta (apenas para debug)
        # try:
        #     account_info = mp.merchant_order().get_all()
        #     print(f"Informações da conta: {account_info}")
        # except Exception as e:
        #     print(f"Erro ao verificar conta: {str(e)}")
        
        # Configuração básica da preferência
        preference_data = {
            "items": [
                {
                    "id": str(item.anuncio.id), # Usar anuncio.id
                    "title": item.anuncio.titulo,
                    "quantity": item.quantidade,
                    "currency_id": "BRL",
                    "unit_price": float(item.anuncio.preco),
                    "description": f"Frete: {item.frete_servico or 'Não selecionado'} - {item.frete_prazo or '0'} dias" # Usar item.frete_servico
                } for item in itens_carrinho
            ],
            "shipments": {
                "cost": float(total_frete),
                "mode": "not_specified"
            },
            "back_urls": {
                "success": "http://localhost:5000/pagamento/sucesso",
                "failure": "http://localhost:5000/pagamento/falha",
                "pending": "http://localhost:5000/pagamento/pendente"
            },
            "payment_methods": {
                "installments": 1,
                "default_installments": 1,
                "excluded_payment_types": [],
                "excluded_payment_methods": []
            },
            "external_reference": f"pedido_{session['user_id']}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            "notification_url": "http://localhost:5000/webhook/mercadopago",
            "statement_descriptor": "VELVEX"
        }
        
        # Adicionar taxa de autenticidade como item separado
        if taxa_autenticidade > 0:
            preference_data["items"].append({
                "id": "taxa_autenticidade",
                "title": "Verificação de Autenticidade",
                "quantity": 1,
                "currency_id": "BRL",
                "unit_price": float(taxa_autenticidade),
                "description": "Garantia de produto original"
            })
        
        print(f"Dados da preferência: {preference_data}")
        
        # Criar preferência
        preference_response = mp.preference().create(preference_data)
        print(f"Resposta completa do Mercado Pago: {preference_response}")
        
        if "response" in preference_response and "id" in preference_response["response"]:
            preference_id = preference_response["response"]["id"]
            init_point = preference_response["response"].get("init_point")
            sandbox_init_point = preference_response["response"].get("sandbox_init_point")
            print(f"Preferência criada com sucesso. ID: {preference_id}")
            print(f"Init Point: {init_point}")
            print(f"Sandbox Init Point: {sandbox_init_point}")
            
            # Verificar métodos de pagamento disponíveis (para debug)
            # payment_methods = preference_response["response"].get("payment_methods", {})
            # print(f"Métodos de pagamento disponíveis: {payment_methods}")
            
            # Verificar se o PIX está disponível (para debug)
            # if "excluded_payment_types" in payment_methods:
            #     print(f"Tipos de pagamento excluídos: {payment_methods['excluded_payment_types']}")
            # if "excluded_payment_methods" in payment_methods:
            #     print(f"Métodos de pagamento excluídos: {payment_methods['excluded_payment_methods']}")
            
        else:
            print(f"Erro na resposta do Mercado Pago: {preference_response}")
            preference_id = None
            
    except Exception as e:
        print(f"Erro detalhado ao criar preferência do Mercado Pago: {str(e)}")
        print(f"Tipo do erro: {type(e)}")
        import traceback
        print(f"Traceback completo: {traceback.format_exc()}")
        preference_id = None
    
    return render_template('checkout.html', 
                         itens_carrinho=itens_carrinho, 
                         total_carrinho=total_carrinho, 
                         total_frete=total_frete, 
                         total_geral=total_geral,
                         taxa_autenticidade=taxa_autenticidade,
                         todos_fretes_selecionados=todos_fretes_selecionados,
                         preference_id=preference_id,
                         mercado_pago_public_key=MERCADO_PAGO_PUBLIC_KEY,
                         valores_cripto=valores_cripto,
                         cotacoes=cotacoes)

@app.route('/pagamento/cripto/<moeda>')
@login_required
def pagamento_cripto(moeda):
    if moeda != 'bitcoin':
        flash('Apenas Bitcoin está disponível no momento.', 'warning')
        return redirect(url_for('checkout_page'))
    """Processa pagamento com criptomoeda"""
    itens_carrinho = ItemCarrinho.query.filter_by(user_id=session['user_id']).all()
    if not itens_carrinho:
        flash('Carrinho vazio.', 'warning')
        return redirect(url_for('carrinho_page'))
    
    total_carrinho = sum(item.anuncio.preco * item.quantidade for item in itens_carrinho if item.anuncio)
    total_frete = sum(item.frete_valor or 0 for item in itens_carrinho)
    
    # Calcular taxas adicionais
    taxa_autenticidade = 20.00  # R$ 20,00 pela verificação de autenticidade
    taxa_intermediacao = total_carrinho * 0.10  # 10% do valor dos produtos pela intermediação
    
    total_geral = total_carrinho + total_frete + taxa_autenticidade + taxa_intermediacao
    
    # Mapeamento de moedas
    moedas_info = {
        "bitcoin": {"nome": "Bitcoin", "simbolo": "BTC", "decimais": 8},
        "ethereum": {"nome": "Ethereum", "simbolo": "ETH", "decimais": 6},
        "cardano": {"nome": "Cardano", "simbolo": "ADA", "decimais": 2},
        "solana": {"nome": "Solana", "simbolo": "SOL", "decimais": 4},
        "polkadot": {"nome": "Polkadot", "simbolo": "DOT", "decimais": 4},
        "litecoin": {"nome": "Litecoin", "simbolo": "LTC", "decimais": 6},
        "chainlink": {"nome": "Chainlink", "simbolo": "LINK", "decimais": 2},
        "polygon": {"nome": "Polygon", "simbolo": "MATIC", "decimais": 2}
    }
    
    if moeda not in moedas_info:
        flash('Criptomoeda não suportada.', 'danger')
        return redirect(url_for('checkout_page'))
    
    valor_cripto = calcular_valor_cripto(total_geral, moeda)
    if not valor_cripto:
        flash('Erro ao calcular valor em criptomoeda.', 'danger')
        return redirect(url_for('checkout_page'))
    
    info_moeda = moedas_info[moeda]
    
    return render_template('pagamento_cripto.html',
                         moeda=moeda,
                         nome_moeda=info_moeda["nome"],
                         simbolo=info_moeda["simbolo"],
                         decimais=info_moeda["decimais"],
                         valor_brl=total_geral,
                         valor_cripto=valor_cripto,
                         cotacao_atual=obter_cotacao_cripto(moeda))

@app.route('/confirmar-pagamento-cripto/<moeda>', methods=['POST'])
@login_required
def confirmar_pagamento_cripto(moeda):
    """Confirma o pagamento com criptomoeda"""
    try:
        itens_carrinho = ItemCarrinho.query.filter_by(user_id=session['user_id']).all()
        if not itens_carrinho:
            flash('Carrinho vazio.', 'danger')
            return redirect(url_for('checkout_page'))
        
        total_carrinho = sum(item.anuncio.preco * item.quantidade for item in itens_carrinho if item.anuncio)
        total_frete = sum(item.frete_valor or 0 for item in itens_carrinho)
        
        # Calcular taxas adicionais
        taxa_autenticidade = 20.00  # R$ 20,00 pela verificação de autenticidade
        taxa_intermediacao = total_carrinho * 0.10  # 10% do valor dos produtos pela intermediação
        
        total_geral = total_carrinho + total_frete + taxa_autenticidade + taxa_intermediacao
        valor_cripto = calcular_valor_cripto(total_geral, moeda)
        
        # Criar pedido (status 'pendente' inicialmente)
        pedido = Pedido(
            user_id=session['user_id'],
            total_brl=total_geral,
            cripto_moeda=moeda,
            cripto_valor=valor_cripto,
            cripto_endereco=SMART_WALLET_ADDRESS, # Usar SMART_WALLET_ADDRESS
            status='pendente'
        )
        db.session.add(pedido)
        db.session.flush() # Garante que pedido.id esteja disponível antes de adicionar ItemPedido

        # Transferir itens do ItemCarrinho para ItemPedido
        for item_carrinho in itens_carrinho:
            item_pedido = ItemPedido(
                pedido_id=pedido.id,
                anuncio_id=item_carrinho.anuncio_id,
                quantidade=item_carrinho.quantidade,
                preco_unitario=item_carrinho.anuncio.preco, # Preço no momento da compra
                frete_servico=item_carrinho.frete_servico,
                frete_valor=item_carrinho.frete_valor,
                frete_prazo=item_carrinho.frete_prazo
            )
            db.session.add(item_pedido)
        
        db.session.commit()
        
        flash(f'Pedido criado! Aguardando confirmação do pagamento em {moeda.upper()}.', 'info')
        return redirect(url_for('meus_pedidos'))
        
    except Exception as e:
        flash(f'Erro ao processar pedido: {str(e)}', 'danger')
        db.session.rollback() # Garante que a transação é desfeita em caso de erro
        return redirect(url_for('checkout_page'))

@app.route('/meus-pedidos')
@login_required
def meus_pedidos():
    """Página para visualizar pedidos do usuário (compras e vendas)"""
    # Carregar 'Minhas Compras' (pedidos onde o usuário é o comprador)
    minhas_compras = Pedido.query.filter_by(user_id=session['user_id']).options(
        # Carrega os itens do pedido e, para cada item, o anúncio associado
        joinedload(Pedido.itens_pedido).joinedload(ItemPedido.anuncio)
    ).order_by(Pedido.data_criacao.desc()).all()

    # Carregar 'Minhas Vendas' (pedidos onde o usuário é o vendedor de algum item)
    # Primeiro, encontre todos os IDs de pedidos que contêm anúncios do usuário logado
    venda_pedido_ids = db.session.query(ItemPedido.pedido_id).join(Anuncio).filter(
        Anuncio.user_id == session['user_id']
    ).distinct().all()
    
    # Extraia os IDs dos pedidos
    venda_pedido_ids = [pid[0] for pid in venda_pedido_ids]

    # Em seguida, carregue os objetos Pedido correspondentes, com seus itens e anúncios
    minhas_vendas = Pedido.query.filter(Pedido.id.in_(venda_pedido_ids)).options(
        joinedload(Pedido.itens_pedido).joinedload(ItemPedido.anuncio)
    ).order_by(Pedido.data_criacao.desc()).all()

    return render_template('meus_pedidos.html', 
                           minhas_compras=minhas_compras,
                           minhas_vendas=minhas_vendas)


@app.route('/admin/pedidos')
@admin_required
def admin_pedidos():
    """Página para administrador verificar pedidos"""
    pedidos = Pedido.query.options(
        joinedload(Pedido.comprador), # Carrega o comprador do pedido
        joinedload(Pedido.itens_pedido).joinedload(ItemPedido.anuncio).joinedload(Anuncio.autor) # Carrega itens, anúncios e seus autores
    ).order_by(Pedido.data_criacao.desc()).all()
    return render_template('admin_pedidos.html', pedidos=pedidos)

@app.route('/admin/confirmar-pagamento/<int:pedido_id>', methods=['POST'])
@admin_required
def confirmar_pagamento_admin(pedido_id):
    """Admin confirma que recebeu o pagamento"""
    try:
        pedido = Pedido.query.get_or_404(pedido_id)
        
        # Garante que apenas pedidos pendentes podem ser confirmados
        if pedido.status != 'pendente':
            flash(f'O pedido #{pedido.id} já foi processado ou não está pendente.', 'warning')
            return redirect(url_for('admin_pedidos'))

        hash_transacao = request.form.get('hash_transacao', '')
        
        pedido.status = 'pago'
        pedido.data_pagamento = datetime.utcnow()
        pedido.hash_transacao = hash_transacao
        
        # Limpar os itens do carrinho do usuário que correspondem aos itens deste pedido
        for item_pedido in pedido.itens_pedido:
            item_carrinho_to_remove = ItemCarrinho.query.filter_by(
                user_id=pedido.user_id, # Usuário do pedido
                anuncio_id=item_pedido.anuncio_id
            ).first()
            if item_carrinho_to_remove:
                db.session.delete(item_carrinho_to_remove)
        
        db.session.commit()
        
        flash(f'Pagamento do pedido #{pedido.id} confirmado!', 'success')
        return redirect(url_for('admin_pedidos'))
        
    except Exception as e:
        db.session.rollback() # Reverte a transação em caso de erro
        flash(f'Erro ao confirmar pagamento: {str(e)}', 'danger')
        return redirect(url_for('admin_pedidos'))

@app.route('/pagamento/sucesso')
@login_required
def pagamento_sucesso():
    payment_id = request.args.get('payment_id')
    status = request.args.get('status')
    external_reference = request.args.get('external_reference')
    
    print(f"Sucesso no pagamento - Detalhes:")
    print(f"Payment ID: {payment_id}")
    print(f"Status: {status}")
    print(f"External Reference: {external_reference}")
    print(f"Query params: {request.args}")
    
    if status == 'approved':
        # Encontre o pedido pendente usando o external_reference ou payment_id
        # Idealmente, o external_reference seria o id do pedido criado anteriormente.
        # Por simplicidade, vamos tentar encontrar um pedido do usuário logado que esteja pendente
        # e que não tenha sido processado por este payment_id ainda.
        pedido_para_atualizar = Pedido.query.filter_by(
            user_id=session['user_id'],
            status='pendente'
            # Poderia adicionar external_reference ou payment_id aqui para ser mais específico
        ).first()

        if pedido_para_atualizar:
            # Se a preferência do Mercado Pago usa um external_reference único que é o ID do Pedido
            # então poderíamos fazer: pedido_para_atualizar = Pedido.query.get(int(external_reference.split('_')[1]))
            # Mas a sua geração de external_reference é 'pedido_{session['user_id']}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}'
            # então seria necessário parsear isso ou ter um campo dedicado no Pedido para o external_reference do MP.
            
            pedido_para_atualizar.status = 'pago'
            pedido_para_atualizar.data_pagamento = datetime.utcnow()
            pedido_para_atualizar.hash_transacao = payment_id # Usar o ID do pagamento do Mercado Pago como hash

            # Limpar os itens do carrinho do usuário que correspondem aos itens deste pedido
            for item_pedido in pedido_para_atualizar.itens_pedido:
                item_carrinho_to_remove = ItemCarrinho.query.filter_by(
                    user_id=session['user_id'],
                    anuncio_id=item_pedido.anuncio_id
                ).first()
                if item_carrinho_to_remove:
                    db.session.delete(item_carrinho_to_remove)

            db.session.commit()
            flash('Pagamento aprovado! Seu pedido foi processado com sucesso.', 'success')
        else:
            flash('Pagamento aprovado, mas não foi possível vincular a um pedido pendente existente.', 'warning')
            print(f"Alerta: Pagamento {payment_id} aprovado, mas nenhum pedido pendente encontrado para o usuário {session['user_id']}.")
    else:
        flash('O pagamento foi recebido, mas ainda está sendo processado.', 'info')
    
    return redirect(url_for('meus_pedidos')) # Redireciona para meus_pedidos

@app.route('/pagamento/falha')
@login_required
def pagamento_falha():
    payment_id = request.args.get('payment_id')
    status = request.args.get('status')
    external_reference = request.args.get('external_reference')
    
    print(f"Falha no pagamento - Detalhes:")
    print(f"Payment ID: {payment_id}")
    print(f"Status: {status}")
    print(f"External Reference: {external_reference}")
    print(f"Query params: {request.args}")
    
    # Opcional: Atualizar o status do pedido para 'cancelado' ou 'falha'
    # Buscar o pedido pelo external_reference (ou como você o vinculou)
    # pedido_para_atualizar = Pedido.query.filter_by(external_reference=external_reference).first()
    # if pedido_para_atualizar:
    #     pedido_para_atualizar.status = 'cancelado' # ou 'falha'
    #     db.session.commit()
    
    flash('Houve um problema com o pagamento. Por favor, tente novamente.', 'danger')
    return redirect(url_for('carrinho_page'))

@app.route('/pagamento/pendente')
@login_required
def pagamento_pendente():
    flash('O pagamento está pendente. Você receberá uma notificação quando for confirmado.', 'warning')
    return redirect(url_for('meus_pedidos')) # Redireciona para meus_pedidos, onde o usuário verá o status

@app.route('/webhook/mercadopago', methods=['POST'])
def webhook_mercadopago():
    try:
        data = request.get_json()
        
        if data["type"] == "payment":
            payment_id = data["data"]["id"]
            payment_info = mp.payment().get(payment_id)
            
            if payment_info["status"] == 200:
                payment_data = payment_info["response"]
                external_reference = payment_data.get("external_reference")
                
                if payment_data["status"] == "approved":
                    # Tentar encontrar o pedido associado a este external_reference
                    # Você precisará ajustar como o external_reference é usado no seu modelo Pedido
                    # Por exemplo, se você o salva em um campo no Pedido
                    # pedido_para_atualizar = Pedido.query.filter_by(external_reference=external_reference).first()
                    # if pedido_para_atualizar:
                    #     pedido_para_atualizar.status = 'pago'
                    #     pedido_para_atualizar.data_pagamento = datetime.utcnow()
                    #     pedido_para_atualizar.hash_transacao = payment_id
                    #     db.session.commit()

                    # Lógica para limpar o carrinho para os itens deste pedido (se não foi feito ainda)
                    # Isso é importante para evitar que o usuário tente comprar os mesmos itens novamente
                    # if pedido_para_atualizar:
                    #    for item_pedido in pedido_para_atualizar.itens_pedido:
                    #        item_carrinho_to_remove = ItemCarrinho.query.filter_by(user_id=pedido_para_atualizar.user_id, anuncio_id=item_pedido.anuncio_id).first()
                    #        if item_carrinho_to_remove:
                    #            db.session.delete(item_carrinho_to_remove)
                    #    db.session.commit()
                    pass # Placeholder para a lógica real do webhook
                
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"Erro no webhook do Mercado Pago: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/finalizar-compra', methods=['POST'])
@login_required
def finalizar_compra_action():
    itens_carrinho = ItemCarrinho.query.options(joinedload(ItemCarrinho.anuncio)).filter_by(user_id=session['user_id']).all()
    if not itens_carrinho:
        flash('O seu carrinho está vazio.', 'warning')
        return redirect(url_for('carrinho_page'))
    
    # Verifica se todos os itens têm frete selecionado
    if not all(item.frete_valor is not None for item in itens_carrinho):
        flash('Você precisa selecionar o frete para todos os itens antes de finalizar a compra.', 'warning')
        return redirect(url_for('carrinho_page'))
    
    # Aqui você implementaria a lógica de finalização da compra
    # Por enquanto, apenas limpa o carrinho e mostra uma mensagem
    # IMPORTANTE: A lógica de criação do Pedido e ItemPedido foi movida para confirmar_pagamento_cripto
    # e/ou tratamento do Mercado Pago.
    
    flash('Compra finalizada com sucesso! Seus itens foram processados.', 'success')
    return redirect(url_for('meus_pedidos')) # Redireciona para meus_pedidos

@app.route('/anuncio/<int:anuncio_id>/fazer-oferta', methods=['POST'])
@login_required
def fazer_oferta_action(anuncio_id):
    anuncio = Anuncio.query.get_or_404(anuncio_id)
    try:
        valor_oferta = float(request.form.get('valor_oferta', '').strip())
        if valor_oferta > anuncio.preco: flash('A oferta não pode ser maior que o preço do anúncio.', 'warning')
        elif valor_oferta < anuncio.preco * 0.7: flash(f'A oferta não pode ser inferior a 30% do preço (R$ {anuncio.preco * 0.7:.2f}).', 'warning')
        else:
            nova_oferta = Oferta(anuncio_id=anuncio.id, comprador_id=session['user_id'], valor_oferta=valor_oferta)
            db.session.add(nova_oferta)
            db.session.flush() # Garante que nova_oferta.id está disponível
            notificacao_para_vendedor = Notification(
                user_id=anuncio.user_id,
                message=f"Você recebeu uma oferta de R$ {valor_oferta:.2f} no seu anúncio '{anuncio.titulo}'.",
                type='new_offer',
                anuncio_id=anuncio.id,
                oferta_id=nova_oferta.id
            )
            db.session.add(notificacao_para_vendedor)
            db.session.commit()
            flash(f'Sua oferta de R$ {valor_oferta:.2f} foi enviada!', 'success')
    except (ValueError, TypeError): flash('Valor da oferta inválido.', 'danger')
    return redirect(url_for('detalhes_anuncio_page', anuncio_id=anuncio_id))

@app.route('/oferta/<int:oferta_id>/aceitar', methods=['POST'])
@login_required
def aceitar_oferta_action(oferta_id):
    oferta = Oferta.query.get_or_404(oferta_id)
    anuncio = oferta.anuncio
    if anuncio.user_id == session['user_id'] and anuncio.status == 'aprovado':
        anuncio.status = 'vendido'
        oferta.status = 'aceita'
        
        # Rejeitar outras ofertas pendentes para o mesmo anúncio
        Oferta.query.filter(
            Oferta.anuncio_id == anuncio.id, 
            Oferta.id != oferta.id,
            Oferta.status == 'pendente'
        ).update({'status': 'rejeitada'})

        notificacao_para_comprador = Notification(
            user_id=oferta.comprador_id,
            message=f"Boas notícias! Sua oferta no item '{anuncio.titulo}' foi aceita. O item agora está reservado para você no seu carrinho.",
            type='offer_accepted',
            anuncio_id=anuncio.id
        )
        db.session.add(notificacao_para_comprador)
        
        # Adicionar o item ao carrinho do comprador se ainda não estiver lá
        if not ItemCarrinho.query.filter_by(user_id=oferta.comprador_id, anuncio_id=anuncio.id).first():
            db.session.add(ItemCarrinho(user_id=oferta.comprador_id, anuncio_id=anuncio.id))

        db.session.commit()
        flash(f'Oferta aceita! O anúncio "{anuncio.titulo}" foi marcado como vendido e movido para o carrinho do comprador.', 'success')
    else:
        flash('Não é possível aceitar esta oferta. O anúncio pode não estar mais disponível ou você não tem permissão.', 'danger')
    return redirect(url_for('minha_conta_page'))

@app.route('/oferta/<int:oferta_id>/rejeitar', methods=['POST'])
@login_required
def rejeitar_oferta_action(oferta_id):
    oferta = Oferta.query.get_or_404(oferta_id)
    if oferta.anuncio.user_id == session['user_id']:
        oferta.status = 'rejeitada'
        notificacao_para_comprador = Notification(
            user_id=oferta.comprador_id,
            message=f"Sua oferta no item '{oferta.anuncio.titulo}' foi rejeitada pelo vendedor.",
            type='offer_rejected',
            anuncio_id=oferta.anuncio.id
        )
        db.session.add(notificacao_para_comprador)
        db.session.commit()
        flash('Oferta rejeitada.', 'info')
    return redirect(url_for('minha_conta_page'))

@app.route('/oferta/<int:oferta_id>/cancelar', methods=['POST'])
@login_required
def cancelar_oferta_action(oferta_id):
    oferta = Oferta.query.get_or_404(oferta_id)
    if oferta.comprador_id == session['user_id']:
        db.session.delete(oferta)
        db.session.commit()
        flash('Sua oferta foi cancelada.', 'info')
    return redirect(url_for('minha_conta_page'))

# --- 11. ROTAS DE CHAT E NOTIFICAÇÕES ---
@app.route('/notificacoes')
@login_required
def notificacoes_page():
    notificacoes_nao_lidas = Notification.query.filter_by(user_id=session['user_id'], is_read=False).order_by(Notification.timestamp.desc()).all()
    notificacoes_lidas = Notification.query.filter_by(user_id=session['user_id'], is_read=True).order_by(Notification.timestamp.desc()).limit(10).all()
    return render_template('notificacoes.html', notificacoes_nao_lidas=notificacoes_nao_lidas, notificacoes_lidas=notificacoes_lidas)

@app.route('/notificacao/<int:notification_id>/abrir', methods=['POST'])
@login_required
def abrir_notificacao_action(notification_id):
    notificacao = Notification.query.get_or_404(notification_id)
    if notificacao.user_id == session['user_id']:
        notificacao.is_read = True
        db.session.commit()
        if notificacao.type == 'chat_report' and session.get('is_admin'):
            return redirect(url_for('anuncio_chat_page', anuncio_id=notificacao.anuncio_id))
        if notificacao.anuncio_id:
            if notificacao.type == 'offer_accepted':
                return redirect(url_for('carrinho_page'))
            return redirect(url_for('detalhes_anuncio_page', anuncio_id=notificacao.anuncio_id))
    return redirect(url_for('notificacoes_page'))

@app.route('/notificacoes/marcar-todas-lidas', methods=['POST'])
@login_required
def marcar_todas_notificacoes_lidas():
    Notification.query.filter_by(user_id=session['user_id'], is_read=False).update({'is_read': True})
    db.session.commit()
    flash('Todas as notificações foram marcadas como lidas.', 'success')
    return redirect(url_for('notificacoes_page'))

@app.route('/meus-chats')
@login_required
def meus_chats_page():
    current_user_id = session['user_id']
    sent_ids = db.session.query(Message.anuncio_id).filter(Message.sender_id == current_user_id).distinct()
    received_ids = db.session.query(Anuncio.id).join(Message).filter(Anuncio.user_id == current_user_id).distinct()
    unique_anuncio_ids = {id[0] for id in sent_ids.union(received_ids)}
    
    conversations_data = []
    for anuncio_id in unique_anuncio_ids:
        anuncio = Anuncio.query.get(anuncio_id)
        if not anuncio: continue
        last_message = Message.query.filter_by(anuncio_id=anuncio_id).order_by(Message.timestamp.desc()).first()
        if not last_message: continue

        other_participant_id = anuncio.user_id if last_message.sender_id != anuncio.user_id else Message.query.filter(Message.anuncio_id==anuncio_id, Message.sender_id != current_user_id).first().sender_id if Message.query.filter(Message.anuncio_id==anuncio_id, Message.sender_id != current_user_id).first() else None
        if not other_participant_id: continue

        other_participant = User.query.get(other_participant_id)
        
        unread_count = Message.query.filter_by(anuncio_id=anuncio_id, sender_id=other_participant.id, is_read=False).count()
        
        conversations_data.append({
            'anuncio': anuncio, 'other_participant': other_participant,
            'last_message_content': last_message.content,
            'last_message_timestamp': last_message.timestamp, 'unread_count': unread_count
        })
    
    sorted_conversations = sorted(conversations_data, key=lambda x: x['last_message_timestamp'], reverse=True)
    return render_template('meus_chats.html', conversations=sorted_conversations)

@app.route('/anuncio/<int:anuncio_id>/chat', methods=['GET', 'POST'])
@login_required
def anuncio_chat_page(anuncio_id):
    anuncio = Anuncio.query.get_or_404(anuncio_id)
    current_user = User.query.get(session['user_id'])
    
    other_participant = None
    if current_user.id != anuncio.user_id:
        other_participant = anuncio.autor
    else:
        first_message_from_other = Message.query.filter(Message.anuncio_id == anuncio.id, Message.sender_id != current_user.id).first()
        if first_message_from_other: other_participant = first_message_from_other.sender
    
    if request.method == 'POST':
        if current_user.chat_banned_until and current_user.chat_banned_until > datetime.utcnow():
            flash(f"Você está suspenso do chat até {current_user.chat_banned_until.strftime('%d/%m/%Y às %H:%M')}.", 'danger')
        else:
            message_content = request.form.get('message_content', '').strip()
            if not message_content: flash('A mensagem não pode ser vazia.', 'danger')
            elif contains_forbidden_info(message_content):
                current_user.strike_count = User.strike_count + 1
                db.session.add(StrikeLog(user_id=current_user.id, anuncio_id=anuncio.id, message_content=message_content))
                if current_user.strike_count >= 3:
                    current_user.chat_banned_until = datetime.utcnow() + timedelta(hours=24)
                    flash('Você atingiu o número máximo de avisos e foi suspenso do chat por 24 horas.', 'danger')
                else:
                    flash(f'AVISO [{current_user.strike_count}/3]: Sua mensagem contém informações proibidas e foi bloqueada.', 'warning')
                db.session.commit()
            else:
                db.session.add(Message(anuncio_id=anuncio_id, sender_id=current_user.id, content=message_content))
                if other_participant:
                    db.session.add(Notification(
                        user_id=other_participant.id,
                        message=f'Você tem uma nova mensagem de {current_user.nome} sobre "{anuncio.titulo}".',
                        type='new_message',
                        anuncio_id=anuncio.id
                    ))
                db.session.commit()
        return redirect(url_for('anuncio_chat_page', anuncio_id=anuncio_id))

    chat_messages = Message.query.filter_by(anuncio_id=anuncio_id).order_by(Message.timestamp.asc()).all()
    if other_participant:
        Message.query.filter_by(anuncio_id=anuncio_id, sender_id=other_participant.id).update({'is_read': True})
        db.session.commit()
    
    return render_template('anuncio_chat.html', anuncio=anuncio, chat_messages=chat_messages, current_user_id=current_user.id, other_participant=other_participant)

@app.route('/chat/report/<int:anuncio_id>/<int:reported_user_id>', methods=['POST'])
@login_required
def report_chat(anuncio_id, reported_user_id):
    reporting_user, anuncio = User.query.get(session['user_id']), Anuncio.query.get_or_404(anuncio_id)
    for admin in User.query.filter_by(is_admin=True).all():
        db.session.add(Notification(
            user_id=admin.id,
            message=f"Denúncia: Usuário '{reporting_user.nome}' (ID: {reporting_user.id}) denunciou o usuário ID {reported_user_id} na conversa do anúncio '{anuncio.titulo}'.",
            type='chat_report', anuncio_id=anuncio.id
        ))
    db.session.commit()
    flash('Denúncia enviada. Nossa equipe irá analisar a conversa.', 'success')
    return redirect(url_for('anuncio_chat_page', anuncio_id=anuncio_id))
    
# --- 12. ROTAS DE ADMIN ---
@app.route('/admin')
@admin_required
def admin_dashboard():
    stats = {
        'num_total_usuarios': User.query.count(),
        'num_anuncios_pendentes': Anuncio.query.filter_by(status='pendente').count(),
        'num_anuncios_aprovados': Anuncio.query.filter_by(status='aprovado').count(),
        'num_anuncios_rejeitados': Anuncio.query.filter_by(status='rejeitado').count()
    }
    return render_template('admin_dashboard.html', **stats)

@app.route('/admin/aprovar-anuncios')
@admin_required
def admin_aprovar_anuncios_page():
    anuncios_pendentes = Anuncio.query.filter_by(status='pendente').order_by(Anuncio.id.asc()).all()
    return render_template('admin_aprovar_anuncios.html', anuncios=anuncios_pendentes)

@app.route('/admin/gerenciar-usuarios')
@admin_required
def admin_gerenciar_usuarios_page():
    sort_by = request.args.get('sort_by', 'id')
    query = User.query.order_by(User.strike_count.desc()) if sort_by == 'strikes' else User.query.order_by(User.id.asc())
    todos_usuarios = query.all()
    num_admins = User.query.filter_by(is_admin=True).count()
    return render_template('admin_gerenciar_usuarios.html', usuarios=todos_usuarios, num_admins=num_admins, current_sort=sort_by)

@app.route('/admin/anuncio/<int:ad_id>/aprovar', methods=['POST'])
@admin_required
def admin_aprovar_anuncio(ad_id):
    anuncio = Anuncio.query.get_or_404(ad_id)
    anuncio.status = 'aprovado'
    db.session.commit()
    flash(f'Anúncio "{anuncio.titulo}" aprovado!', 'success')
    return redirect(url_for('admin_aprovar_anuncios_page'))

@app.route('/admin/anuncio/<int:ad_id>/rejeitar', methods=['POST'])
@admin_required
def admin_rejeitar_anuncio(ad_id):
    anuncio = Anuncio.query.get_or_404(ad_id)
    anuncio.status = 'rejeitado'
    db.session.commit()
    flash(f'Anúncio "{anuncio.titulo}" rejeitado.', 'info')
    return redirect(url_for('admin_aprovar_anuncios_page'))

@app.route('/admin/usuario/<int:user_id>/toggle-admin', methods=['POST'])
@admin_required
def admin_toggle_admin_status(user_id):
    user = User.query.get_or_404(user_id)
    if user.id != session['user_id']:
        user.is_admin = not user.is_admin
        db.session.commit()
        flash(f'Status de admin de {user.nome} alterado.', 'success')
    else: flash('Não é possível alterar seu próprio status de admin.', 'warning')
    return redirect(url_for('admin_gerenciar_usuarios_page'))

@app.route('/admin/usuario/<int:user_id>/toggle-ban', methods=['POST'])
@admin_required
def admin_toggle_ban_status(user_id):
    user = User.query.get_or_404(user_id)
    if user.id != session['user_id']:
        user.is_banned = not user.is_banned
        db.session.commit()
        flash(f'Status de banimento de {user.nome} alterado.', 'success')
    else: flash('Não é possível banir a si mesmo.', 'warning')
    return redirect(url_for('admin_gerenciar_usuarios_page'))

@app.route('/admin/usuario/<int:user_id>/delete', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id != session['user_id']:
        db.session.delete(user)
        db.session.commit()
        flash(f'Usuário {user.nome} deletado permanentemente.', 'success')
    else: flash('Não é possível deletar sua própria conta de admin.', 'danger')
    return redirect(url_for('admin_gerenciar_usuarios_page'))

@app.route('/admin/usuario/<int:user_id>/reset-strikes', methods=['POST'])
@admin_required
def admin_reset_strikes(user_id):
    user = User.query.get_or_404(user_id)
    user.strike_count = 0
    user.chat_banned_until = None
    StrikeLog.query.filter_by(user_id=user.id).delete()
    db.session.commit()
    flash(f'Os avisos e a suspensão de chat do usuário {user.nome} foram resetados.', 'success')
    return redirect(url_for('admin_gerenciar_usuarios_page'))

@app.route('/admin/usuario/<int:user_id>/strikes')
@admin_required
def admin_view_strikes_page(user_id):
    user = User.query.get_or_404(user_id)
    strike_logs = StrikeLog.query.filter_by(user_id=user.id).order_by(StrikeLog.timestamp.desc()).all()
    return render_template('admin_view_strikes.html', user=user, strike_logs=strike_logs)

# --- 13. ROTAS DA API DE FRETE ---
@app.route('/api/calcular-frete', methods=['POST'])
@login_required
def calcular_frete_api():
    data = request.get_json()
    cep_destino_comprador = data.get('cep_destino')
    item_id = data.get('item_id')
    anuncio_id = data.get('anuncio_id')
    anuncio = None
    
    if item_id:
        item = ItemCarrinho.query.filter_by(id=item_id, user_id=session['user_id']).first()
        if not item:
            return jsonify({"error": "Item do carrinho não encontrado."}), 404
        anuncio = Anuncio.query.get(item.anuncio_id)
    elif anuncio_id:
        anuncio = Anuncio.query.get(anuncio_id)
        
    if not cep_destino_comprador or not anuncio:
        return jsonify({"error": "CEP de destino e ID do item do carrinho ou anúncio são obrigatórios."}), 400
        
    if not anuncio:
        return jsonify({"error": "Anúncio não encontrado."}), 404
        
    cep_origem_vendedor = anuncio.autor.cep
    if not cep_origem_vendedor:
        return jsonify({"error": "O vendedor não registou um CEP de origem. Não é possível calcular o frete."}), 400
        
    opcoes_trecho1 = calcular_frete_melhor_envio(cep_origem_vendedor, INTERMEDIARY_CEP, anuncio)
    if not opcoes_trecho1:
        return jsonify({"error": "Não foi possível calcular o frete do vendedor para o nosso centro. Verifique o CEP de origem do vendedor."}), 500
        
    opcoes_trecho2 = calcular_frete_melhor_envio(INTERMEDIARY_CEP, cep_destino_comprador, anuncio)
    if not opcoes_trecho2:
        return jsonify({"error": "Não foi possível calcular o frete para o seu CEP. Verifique o CEP de destino e tente novamente."}), 500
        
    opcoes_finais = []
    mapa_trecho2 = {opt['id']: opt for opt in opcoes_trecho2}
    prazo_manuseio = 2
    
    for opt1 in opcoes_trecho1:
        if opt1['id'] in mapa_trecho2:
            opt2 = mapa_trecho2[opt1['id']]
            preco_total = float(opt1.get('price', 0)) + float(opt2.get('price', 0))
            prazo_total = int(opt1.get('delivery_time', 0)) + int(opt2.get('delivery_time', 0)) + prazo_manuseio + 5  # Adiciona 5 dias ao prazo
            opcoes_finais.append({
                "id": opt1['id'],
                "servico": opt1['name'],
                "valor": round(preco_total, 2),
                "prazo": prazo_total,
                "empresa": opt1['company']
            })
            
    if not opcoes_finais:
        return jsonify({"error": "Nenhuma opção de frete em comum foi encontrada para a rota completa."}), 404
        
    return jsonify({"opcoes": opcoes_finais})

@app.route('/api/selecionar-frete', methods=['POST'])
@login_required
def selecionar_frete_api():
    data = request.get_json()
    item_id = data.get('item_id')
    frete_valor = data.get('frete_valor')
    frete_prazo = data.get('frete_prazo')
    frete_servico = data.get('frete_servico')
    if not all([item_id, frete_valor is not None, frete_prazo is not None, frete_servico]):
        return jsonify({"success": False, "error": "Dados incompletos."}), 400
    item_carrinho = ItemCarrinho.query.filter_by(id=item_id, user_id=session['user_id']).first()
    if not item_carrinho:
        return jsonify({"success": False, "error": "Item do carrinho não encontrado."}), 404
    try:
        item_carrinho.frete_valor = float(frete_valor)
        item_carrinho.frete_prazo = int(frete_prazo)
        item_carrinho.frete_servico = str(frete_servico)
        db.session.commit()
        return jsonify({"success": True, "message": "Frete selecionado com sucesso."})
    except Exception as e:
        db.session.rollback()
        print(f"Erro ao guardar frete: {e}")
        return jsonify({"success": False, "error": "Erro ao guardar a opção de frete na base de dados."}), 500

@app.route('/verificar-conta-mercadopago')
def verificar_conta_mercadopago():
    try:
        # Verificar informações da conta
        account_info = mp.merchant_order().get_all()
        print(f"Informações da conta: {account_info}")
        
        # Verificar informações do usuário
        user_info = mp.user().get()
        print(f"Informações do usuário: {user_info}")
        
        return jsonify({
            "account_info": account_info,
            "user_info": user_info
        })
    except Exception as e:
        print(f"Erro ao verificar conta: {str(e)}")
        return jsonify({"error": str(e)})

# --- 14. INICIALIZAÇÃO DA APLICAÇÃO ---
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    # app.run(debug=True) # Removido para usar a função iniciar_verificacao_automatica

def verificar_transacao_ethereum(hash_transacao):
    """Verifica uma transação Ethereum usando Etherscan API"""
    try:
        url = f"https://api.etherscan.io/api"
        params = {
            "module": "proxy",
            "action": "eth_getTransactionByHash",
            "txhash": hash_transacao,
            "apikey": ETHERSCAN_API_KEY
        }
        response = requests.get(url, params=params)
        data = response.json()
        
        if data.get("result"):
            tx = data["result"]
            # Verificar se a transação foi para o endereço correto
            if tx["to"].lower() == SMART_WALLET_ADDRESS.lower():
                # Verificar se a transação foi confirmada
                if tx["blockNumber"] is not None:
                    return True, tx
        return False, None
    except Exception as e:
        print(f"Erro ao verificar transação Ethereum: {str(e)}")
        return False, None

def verificar_transacao_bitcoin(txid):
    """Verifica uma transação Bitcoin usando Mempool.space API"""
    try:
        url = f"{BITCOIN_API_URL}/tx/{txid}"
        response = requests.get(url)
        data = response.json()
        
        # Verificar se a transação tem confirmações
        if data.get("status", {}).get("confirmed"):
            # Verificar outputs para o endereço correto
            for output in data.get("vout", []):
                if output.get("scriptpubkey_address") == SMART_WALLET_ADDRESS:
                    return True, data
        return False, None
    except Exception as e:
        print(f"Erro ao verificar transação Bitcoin: {str(e)}")
        return False, None

def verificar_transacao_polygon(hash_transacao):
    """Verifica uma transação Polygon usando PolygonScan API"""
    try:
        url = f"https://api.polygonscan.com/api"
        params = {
            "module": "proxy",
            "action": "eth_getTransactionByHash",
            "txhash": hash_transacao,
            "apikey": POLYGON_API_KEY
        }
        response = requests.get(url, params=params)
        data = response.json()
        
        if data.get("result"):
            tx = data["result"]
            if tx["to"].lower() == SMART_WALLET_ADDRESS.lower():
                if tx["blockNumber"] is not None:
                    return True, tx
        return False, None
    except Exception as e:
        print(f"Erro ao verificar transação Polygon: {str(e)}")
        return False, None

def verificar_pagamento_automatico(pedido_id):
    """Verifica automaticamente se um pagamento foi recebido"""
    with app.app_context(): # Usar app_context para operações de DB em threads
        try:
            pedido = Pedido.query.get(pedido_id)
            if not pedido or pedido.status != 'pendente':
                return
            
            # Verificar baseado na criptomoeda
            if pedido.cripto_moeda == 'ethereum':
                # Verificar transações recentes para o endereço
                if ETHERSCAN_API_KEY != "YourEtherscanAPIKey":
                    url = f"https://api.etherscan.io/api"
                    params = {
                        "module": "account",
                        "action": "txlist",
                        "address": SMART_WALLET_ADDRESS,
                        "startblock": 0,
                        "endblock": 99999999,
                        "sort": "desc",
                        "apikey": ETHERSCAN_API_KEY
                    }
                    response = requests.get(url, params=params)
                    data = response.json()
                    
                    if data.get("result"):
                        for tx in data["result"]:
                            # Verificar se o valor corresponde
                            valor_wei = int(tx["value"])
                            valor_eth = valor_wei / 10**18
                            
                            if abs(valor_eth - pedido.cripto_valor) < 0.0001:  # Tolerância
                                # Verificar se é uma transação recente
                                if int(tx["timeStamp"]) > (time.time() - 3600):  # Última hora
                                    confirmar_pagamento_automatico(pedido, tx["hash"])
                                    break
                else:
                    print(f"Chave da API Etherscan não configurada para pedido #{pedido_id}")
            
            elif pedido.cripto_moeda == 'bitcoin':
                # Verificar transações Bitcoin (API Mempool.space - compatível com Bech32)
                try:
                    url = f"{BITCOIN_API_URL}/address/{SMART_WALLET_ADDRESS}/txs"
                    response = requests.get(url, timeout=10)
                    data = response.json()
                    
                    for tx in data:
                        for output in tx.get("vout", []):
                            if output.get("scriptpubkey_address") == SMART_WALLET_ADDRESS:
                                valor_btc = output.get("value", 0) / 100000000  # Converter de satoshis
                                if abs(valor_btc - pedido.cripto_valor) < 0.00000001:  # Tolerância
                                    if tx.get("status", {}).get("confirmed"):
                                        confirmar_pagamento_automatico(pedido, tx["txid"])
                                        return
                except Exception as e:
                    print(f"Erro ao verificar Bitcoin para pedido #{pedido_id}: {str(e)}")
            
            elif pedido.cripto_moeda == 'polygon':
                # Verificar transações Polygon
                if POLYGON_API_KEY != "YourPolygonAPIKey":
                    url = f"https://api.polygonscan.com/api"
                    params = {
                        "module": "account",
                        "action": "txlist",
                        "address": SMART_WALLET_ADDRESS,
                        "startblock": 0,
                        "endblock": 99999999,
                        "sort": "desc",
                        "apikey": POLYGON_API_KEY
                    }
                    response = requests.get(url, params=params)
                    data = response.json()
                    
                    if data.get("result"):
                        for tx in data["result"]:
                            valor_wei = int(tx["value"])
                            valor_matic = valor_wei / 10**18
                            
                            if abs(valor_matic - pedido.cripto_valor) < 0.0001:
                                if int(tx["timeStamp"]) > (time.time() - 3600):
                                    confirmar_pagamento_automatico(pedido, tx["hash"])
                                    break
                else:
                    print(f"Chave da API Polygon não configurada para pedido #{pedido_id}")
        except Exception as e:
            print(f"Erro na verificação automática para pedido #{pedido_id}: {str(e)}")

def confirmar_pagamento_automatico(pedido, hash_transacao):
    """Confirma automaticamente um pagamento recebido"""
    with app.app_context(): # Usar app_context para operações de DB em threads
        try:
            print(f"💰 Pagamento detectado para pedido #{pedido.id}!")
            print(f"   Hash: {hash_transacao}")
            print(f"   Valor: {pedido.cripto_valor} {pedido.cripto_moeda.upper()}")
            
            pedido.status = 'pago'
            pedido.data_pagamento = datetime.utcnow()
            pedido.hash_transacao = hash_transacao
            
            # Limpar carrinho do usuário para os itens desta transação
            # Iterar pelos itens do pedido confirmado e remover do carrinho
            for item_pedido in pedido.itens_pedido:
                item_carrinho_to_remove = ItemCarrinho.query.filter_by(
                    user_id=pedido.user_id, # Usuário do pedido
                    anuncio_id=item_pedido.anuncio_id
                ).first()
                if item_carrinho_to_remove:
                    db.session.delete(item_carrinho_to_remove)
            
            db.session.commit()
            
            print(f"✅ Pagamento automático confirmado para pedido #{pedido.id}")
            print(f"   Carrinho do usuário {pedido.user_id} foi limpo para os itens do pedido.")
        except Exception as e:
            print(f"❌ Erro ao confirmar pagamento automático para pedido #{pedido.id}: {str(e)}")
            db.session.rollback()

def verificar_pedidos_pendentes():
    """Verifica todos os pedidos pendentes periodicamente"""
    print("🔄 Iniciando verificação automática de pagamentos em criptomoedas...")
    while True:
        try:
            with app.app_context(): # Usar app_context para operações de DB
                pedidos_pendentes = Pedido.query.filter_by(status='pendente').all()
                if pedidos_pendentes:
                    print(f"📋 Verificando {len(pedidos_pendentes)} pedidos pendentes...")
                    for pedido in pedidos_pendentes:
                        print(f"🔍 Verificando pedido #{pedido.id} - {pedido.cripto_moeda.upper()}")
                        # Carregar itens do pedido para que confirmar_pagamento_automatico possa acessá-los
                        db.session.refresh(pedido) # Recarregar o pedido para garantir que itens_pedido estejam disponíveis
                        verificar_pagamento_automatico(pedido.id)
                else:
                    print("✅ Nenhum pedido pendente encontrado.")
            print("⏰ Aguardando 60 segundos para próxima verificação...")
            time.sleep(60)  # Verificar a cada 1 minuto
        except Exception as e:
            print(f"❌ Erro na verificação periódica: {str(e)}")
            print("⏰ Tentando novamente em 60 segundos...")
            time.sleep(60)

# Iniciar thread de verificação automática
def iniciar_verificacao_automatica():
    thread = Thread(target=verificar_pedidos_pendentes, daemon=True)
    thread.start()

@app.route('/verificar-transacao', methods=['POST'])
@login_required
def verificar_transacao():
    """Verifica manualmente uma transação"""
    try:
        hash_transacao = request.form.get('hash_transacao')
        cripto_moeda = request.form.get('cripto_moeda')
        
        if not hash_transacao or not cripto_moeda:
            return jsonify({"success": False, "error": "Hash e criptomoeda são obrigatórios"})
        
        # Verificar baseado na criptomoeda
        if cripto_moeda == 'ethereum':
            confirmado, dados = verificar_transacao_ethereum(hash_transacao)
        elif cripto_moeda == 'bitcoin':
            confirmado, dados = verificar_transacao_bitcoin(hash_transacao)
        elif cripto_moeda == 'polygon':
            confirmado, dados = verificar_transacao_polygon(hash_transacao)
        else:
            return jsonify({"success": False, "error": "Criptomoeda não suportada"})
        
        if confirmado:
            return jsonify({
                "success": True, 
                "message": "Transação confirmada!",
                "dados": dados
            })
        else:
            return jsonify({
                "success": False, 
                "error": "Transação não encontrada ou não confirmada"
            })
            
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/webhook/blockchain', methods=['POST'])
def webhook_blockchain():
    """Webhook para receber notificações de transações"""
    try:
        data = request.get_json()
        
        # Processar notificação baseada na blockchain
        if data.get("network") == "ethereum":
            # Processar notificação Ethereum
            pass
        elif data.get("network") == "bitcoin":
            # Processar notificação Bitcoin
            pass
        
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"Erro no webhook do Mercado Pago: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# Inicializar verificação automática quando a aplicação iniciar
if __name__ == '__main__':
    iniciar_verificacao_automatica()
    app.run(debug=True)

def calcular_frete(cep_origem, cep_destino):
    """Calcula o frete usando a API do ViaCEP"""
    try:
        # Limpa os CEPs (remove caracteres não numéricos)
        cep_origem = re.sub(r'\D', '', cep_origem)
        cep_destino = re.sub(r'\D', '', cep_destino)
        
        # Validação básica dos CEPs
        if not cep_origem or not cep_destino or len(cep_origem) != 8 or len(cep_destino) != 8:
            print(f"CEPs inválidos - Origem: {cep_origem}, Destino: {cep_destino}")
            return None
            
        # Busca informações do CEP de origem
        origem_url = f'https://viacep.com.br/ws/{cep_origem}/json/'
        origem_response = requests.get(origem_url)
        origem_data = origem_response.json()
        
        if 'erro' in origem_data:
            print(f"Erro ao buscar CEP de origem {cep_origem}: {origem_data.get('erro')}")
            return None
            
        # Busca informações do CEP de destino
        destino_url = f'https://viacep.com.br/ws/{cep_destino}/json/'
        destino_response = requests.get(destino_url)
        destino_data = destino_response.json()
        
        if 'erro' in destino_data:
            print(f"Erro ao buscar CEP de destino {cep_destino}: {destino_data.get('erro')}")
            return None
            
        # Calcula a distância usando a API do Google Maps
        origem = f"{origem_data['logradouro']}, {origem_data['bairro']}, {origem_data['localidade']}, {origem_data['uf']}"
        destino = f"{destino_data['logradouro']}, {destino_data['bairro']}, {destino_data['localidade']}, {destino_data['uf']}"
        
        # Se não tiver API key do Google, usa cálculo simplificado
        if not os.getenv('GOOGLE_MAPS_API_KEY'):
            # Cálculo simplificado baseado em região
            regioes = {
                'Norte': ['AC', 'AP', 'AM', 'PA', 'RO', 'RR', 'TO'],
                'Nordeste': ['AL', 'BA', 'CE', 'MA', 'PB', 'PE', 'PI', 'RN', 'SE'],
                'Centro-Oeste': ['DF', 'GO', 'MT', 'MS'],
                'Sudeste': ['ES', 'MG', 'RJ', 'SP'],
                'Sul': ['PR', 'RS', 'SC']
            }
            
            origem_regiao = next((reg for reg, ufs in regioes.items() if origem_data['uf'] in ufs), 'Desconhecida')
            destino_regiao = next((reg for reg, ufs in regioes.items() if destino_data['uf'] in ufs), 'Desconhecida')
            
            # Valores base por região
            valores_base = {
                'Norte': 50.0,
                'Nordeste': 40.0,
                'Centro-Oeste': 35.0,
                'Sudeste': 25.0,
                'Sul': 30.0
            }
            
            # Se for mesma região, frete mais barato
            if origem_regiao == destino_regiao:
                return valores_base[origem_regiao]
            
            # Se for região diferente, soma os valores
            return valores_base[origem_regiao] + valores_base[destino_regiao]
        
        # Se tiver API key do Google, usa cálculo real
        api_key = os.getenv('GOOGLE_MAPS_API_KEY')
        url = f'https://maps.googleapis.com/maps/api/distancematrix/json?origins={origem}&destinations={destino}&key={api_key}'
        response = requests.get(url)
        data = response.json()
        
        if data['status'] == 'OK':
            distancia = data['rows'][0]['elements'][0]['distance']['value'] / 1000  # Converte para km
            return round(distancia * 2, 2)  # R$2 por km
            
        return None
        
    except Exception as e:
        print(f"Erro ao calcular frete: {str(e)}")
        return None

@app.route('/calcular-frete', methods=['POST'])
def calcular_frete_route():
    try:
        data = request.get_json()
        cep_origem = data.get('cep_origem')
        cep_destino = data.get('cep_destino')
        
        if not cep_origem or not cep_destino:
            return jsonify({'error': 'CEPs de origem e destino são obrigatórios'}), 400
            
        # Busca o CEP do vendedor no banco de dados
        anuncio = Anuncio.query.get(data.get('anuncio_id'))
        if not anuncio:
            return jsonify({'error': 'Anúncio não encontrado'}), 404
            
        vendedor = User.query.get(anuncio.user_id)
        if not vendedor or not vendedor.cep:
            return jsonify({'error': 'CEP do vendedor não encontrado'}), 404
            
        # Usa o CEP do vendedor como origem
        frete = calcular_frete(vendedor.cep, cep_destino)
        
        if frete is None:
            return jsonify({'error': 'Não foi possível calcular o frete'}), 400
            
        return jsonify({'frete': frete})
        
    except Exception as e:
        print(f"Erro ao calcular frete: {str(e)}")
        return jsonify({'error': 'Erro ao calcular frete'}), 500

@app.route('/calcular-frete-anuncio/<int:anuncio_id>', methods=['POST'])
def calcular_frete_anuncio(anuncio_id):
    try:
        data = request.get_json()
        cep_destino = data.get('cep')
        
        if not cep_destino:
            return jsonify({'error': 'CEP não fornecido'}), 400
            
        anuncio = Anuncio.query.get_or_404(anuncio_id)
        
        if not anuncio.autor.cep:
            return jsonify({'error': 'Vendedor não possui CEP cadastrado'}), 400
            
        if not all([anuncio.largura, anuncio.altura, anuncio.comprimento, anuncio.peso]):
            return jsonify({'error': 'Anúncio não possui todas as dimensões necessárias'}), 400
            
        opcoes_frete = calcular_frete_melhor_envio(anuncio.autor.cep, cep_destino, anuncio)
        
        if not opcoes_frete:
            return jsonify({'error': 'Não foi possível calcular o frete'}), 400
            
        return jsonify(opcoes_frete)
        
    except Exception as e:
        print(f"Erro ao calcular frete: {str(e)}")
        return jsonify({'error': 'Erro ao calcular frete'}), 500

@app.route('/compra-admin', methods=['POST'])
@admin_required
def compra_admin():
    itens_carrinho = ItemCarrinho.query.options(joinedload(ItemCarrinho.anuncio)).filter_by(user_id=session['user_id']).all()
    if not itens_carrinho:
        flash('O carrinho está vazio.', 'warning')
        return redirect(url_for('checkout_page'))

    total_carrinho = sum(item.anuncio.preco * item.quantidade for item in itens_carrinho if item.anuncio)
    total_frete = sum(item.frete_valor or 0 for item in itens_carrinho)
    taxa_autenticidade = 20.00
    total_geral = total_carrinho + total_frete + taxa_autenticidade

    # Cria o pedido como pago
    pedido = Pedido(
        user_id=session['user_id'],
        total_brl=total_geral,
        status='pago',
        data_pagamento=datetime.utcnow()
    )
    db.session.add(pedido)
    db.session.flush()

    for item in itens_carrinho:
        item_pedido = ItemPedido(
            pedido_id=pedido.id,
            anuncio_id=item.anuncio_id,
            quantidade=item.quantidade,
            preco_unitario=item.anuncio.preco,
            frete_servico=item.frete_servico,
            frete_valor=item.frete_valor,
            frete_prazo=item.frete_prazo
        )
        db.session.add(item_pedido)
        db.session.delete(item)
    db.session.commit()
    flash('Compra admin realizada com sucesso! Pedido criado e marcado como pago.', 'success')
    return redirect(url_for('meus_pedidos'))

@app.route('/admin/confirmar-pagamento-bitcoin/<int:pedido_id>', methods=['POST'])
@admin_required
def confirmar_pagamento_bitcoin_admin(pedido_id):
    """Rota para admin confirmar pagamento via Bitcoin"""
    try:
        pedido = Pedido.query.get_or_404(pedido_id)
        hash_transacao = request.form.get('hash_transacao')
        
        if not hash_transacao:
            flash('Hash da transação é obrigatório', 'error')
            return redirect(url_for('admin_pedidos'))
            
        # Verificar a transação Bitcoin
        confirmado, dados = verificar_transacao_bitcoin(hash_transacao)
        
        if confirmado:
            # Atualizar status do pedido
            pedido.status = 'pago'
            pedido.data_pagamento = datetime.utcnow()
            pedido.hash_transacao = hash_transacao
            
            # Limpar carrinho do usuário
            for item_pedido in pedido.itens_pedido:
                item_carrinho = ItemCarrinho.query.filter_by(
                    user_id=pedido.user_id,
                    anuncio_id=item_pedido.anuncio_id
                ).first()
                if item_carrinho:
                    db.session.delete(item_carrinho)
            
            db.session.commit()
            flash('Pagamento Bitcoin confirmado com sucesso!', 'success')
        else:
            flash('Transação Bitcoin não encontrada ou não confirmada', 'error')
            
        return redirect(url_for('admin_pedidos'))
        
    except Exception as e:
        flash(f'Erro ao confirmar pagamento: {str(e)}', 'error')
        return redirect(url_for('admin_pedidos'))

def verificar_transacao_bitcoin(txid):
    """Verifica uma transação Bitcoin usando a API do Mempool.space"""
    try:
        # Usar a API do Mempool.space (compatível com endereços Bech32)
        url = f"{BITCOIN_API_URL}/tx/{txid}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if response.status_code == 200:
            # Verificar se a transação está confirmada
            if data.get("status", {}).get("confirmed"):
                return True, {
                    "hash": txid,
                    "valor": data.get("fee", 0) / 100000000,  # Converter de satoshis
                    "confirmacoes": data.get("status", {}).get("block_height", 0),
                    "data": datetime.fromtimestamp(data.get("status", {}).get("block_time", 0))
                }
        return False, None
    except Exception as e:
        print(f"Erro ao verificar transação Bitcoin: {str(e)}")
        return False, None
