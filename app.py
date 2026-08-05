import os
import io
import re
import csv
import unicodedata
import pandas as pd
from flask import Flask, render_template_string, request, jsonify
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

# --- CONFIGURAÇÃO DO BANCO DE DADOS (NUVEM OU LOCAL) ---
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL", "sqlite:///brisas_nuvem.db")
if app.config['SQLALCHEMY_DATABASE_URI'].startswith("postgres://"):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- MODELO DA BASE DE DADOS ---
class MedidorBase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    unidade = db.Column(db.String(100))
    tipo = db.Column(db.String(50))
    medidor_visual = db.Column(db.String(100))
    medidor_dna = db.Column(db.String(100), unique=True)
    fracao = db.Column(db.Float, default=0.0)
    taxa = db.Column(db.Float, default=7.90)
    l_anterior = db.Column(db.Float, default=0.0)

with app.app_context():
    db.create_all()

# --- INTELIGÊNCIA MATEMÁTICA ---
class MotorFaturamento:
    @staticmethod
    def classificar_tipo(unidade):
        u = str(unidade).upper()
        u = ''.join(c for c in unicodedata.normalize('NFKD', u) if not unicodedata.combining(c))
        comerciais = ['LOJA', 'RESTAURANTE', 'EMPORIO', 'CAFE', 'SALAO', 'GOURMET', 'COMERCIAL', 'QUIOSQUE', 'CYBER', 'KITCLEAN', 'LAVANDERIA', 'OMO', 'TANQUES']
        for p in comerciais:
            if p in u: return 'COMERCIAL'
        return 'RESIDENCIAL'

    @staticmethod
    def limpar_id_medidor(val):
        """DNA Numérico: Ignora letras para o cruzamento perfeito"""
        if pd.isna(val) or val is None: return ""
        s = str(val).upper().split('.')[0].strip()
        s = re.sub(r'[^0-9]', '', s) 
        return s.lstrip('0')

    @staticmethod
    def calcular_caesb_cascata(m3):
        """Cascata Oficial CAESB"""
        if m3 <= 0: return 0.0
        f1, f2, f3, f4, f5 = 28.91, 29.76, 68.74, 142.50, 320.55
        if m3 <= 7: return m3 * 4.13
        elif m3 <= 13: return f1 + ((m3 - 7) * 4.96)
        elif m3 <= 20: return f1 + f2 + ((m3 - 13) * 9.82)
        elif m3 <= 30: return f1 + f2 + f3 + ((m3 - 20) * 14.25)
        elif m3 <= 45: return f1 + f2 + f3 + f4 + ((m3 - 30) * 21.37)
        else: return f1 + f2 + f3 + f4 + f5 + ((m3 - 45) * 27.77)

    @staticmethod
    def limpar_valor_leitura(val):
        if pd.isna(val) or val is None or str(val).strip() == '': return 0.0
        s = str(val).replace('"', '').replace("'", '').replace('R$', '').strip()
        if ',' in s: 
            if '.' in s: s = s.replace('.', '')
            s = s.replace(',', '.')
        try: return float(s)
        except: return 0.0

    @staticmethod
    def limpa_nome_coluna(nome):
        s = str(nome).upper().strip()
        s = ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))
        return re.sub(r'[^A-Z0-9_]', '', s)

    @staticmethod
    def ler_ficheiro_memoria(file_bytes, filename):
        """Leitor God Mode: Lê arquivos destruídos e HTMLs falsos da telemetria diretamente na nuvem."""
        if filename.lower().endswith(('.xls', '.xlsx')):
            try:
                df = pd.read_excel(io.BytesIO(file_bytes), dtype=str)
                if not df.empty and len(df.columns) > 1: return df
            except: pass

        texto = ""
        for enc in ['utf-8', 'latin1', 'utf-16', 'utf-16le', 'cp1252', 'iso-8859-1', 'utf-8-sig']:
            try:
                texto = file_bytes.decode(enc)
                if len(texto) > 20: break
            except: continue

        if '<table' in texto.lower() or '<tr>' in texto.lower():
            try:
                dfs = pd.read_html(io.StringIO(texto), decimal=',', thousands='.')
                df = max(dfs, key=len)
                if not df.empty and len(df.columns) > 2: return df.astype(str)
            except: pass

        for sep in [';', ',', '\t']:
            try:
                leitor = csv.reader(io.StringIO(texto), delimiter=sep, quotechar='"')
                linhas = [linha for linha in leitor if len(linha) > 0]
                if len(linhas) > 1 and len(linhas[0]) > 1:
                    max_cols = max(len(l) for l in linhas)
                    linhas_pads = [l + [''] * (max_cols - len(l)) for l in linhas]
                    cabecalho = [str(h).strip() if str(h).strip() != '' else f"COL_{i}" for i, h in enumerate(linhas_pads[0])]
                    df = pd.DataFrame(linhas_pads[1:], columns=cabecalho)
                    if not df.empty and len(df.columns) > 1: return df
            except: continue
        raise ValueError("O formato do ficheiro é irreconhecível. Salve como CSV no Excel e tente novamente.")

# ==========================================
# INTERFACE GRÁFICA WEB EMBUTIDA (HTML/CSS/JS)
# ==========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gestão CAESB | Brisas do Lago</title>
    <!-- Bibliotecas Mágicas para PDF e Excel direto do Navegador -->
    <script src="https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf-autotable/3.5.28/jspdf.plugin.autotable.min.js"></script>
    <style>
        :root { --bg: #121212; --panel: #1e1e1e; --card: #2c3e50; --gold: #b8860b; --blue: #2980b9; --green: #27ae60; --red: #c0392b; --text: #ecf0f1; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: var(--bg); color: var(--text); margin: 0; padding: 20px; }
        .container { max-width: 1450px; margin: auto; }
        
        /* Dashboard Cards */
        .dashboard { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; }
        .card { background: var(--card); padding: 20px; border-radius: 10px; text-align: center; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        .card h3 { margin: 0 0 10px 0; font-size: 14px; color: #bdc3c7; text-transform: uppercase; }
        .card h1 { margin: 0; font-size: 28px; }
        .card.orange h1 { color: #f39c12; }
        .card.red h1 { color: #e74c3c; }
        
        /* Controles */
        .controls { background: var(--panel); padding: 20px; border-radius: 10px; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; margin-bottom: 20px; gap: 15px; }
        .input-group { display: flex; flex-direction: column; gap: 5px; }
        input[type="number"], input[type="text"] { padding: 10px; border-radius: 5px; border: 1px solid #444; background: #333; color: white; font-weight: bold; }
        
        button { padding: 12px 20px; border: none; border-radius: 6px; font-weight: bold; cursor: pointer; color: white; transition: 0.2s; display: flex; align-items: center; gap: 8px; font-size: 14px; }
        .btn-gold { background: var(--gold); } .btn-gold:hover { filter: brightness(1.2); }
        .btn-blue { background: var(--blue); } .btn-blue:hover { filter: brightness(1.2); }
        .btn-green { background: var(--green); } .btn-green:hover { filter: brightness(1.2); }
        .btn-red { background: var(--red); } .btn-red:hover { filter: brightness(1.2); }
        
        /* Tabela */
        .table-container { background: var(--panel); border-radius: 10px; padding: 15px; overflow-x: auto; max-height: 500px; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th { background: #34495e; color: white; padding: 12px; position: sticky; top: 0; text-align: center; }
        td { padding: 10px; text-align: center; border-bottom: 1px solid #333; }
        tr:hover { background: #34495e; }
        .type-res { color: #3498db; font-weight: bold; }
        .type-com { color: #f1c40f; font-weight: bold; }
        
        /* Loading Overlay */
        #loading { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 999; justify-content: center; align-items: center; flex-direction: column; }
        .spinner { border: 6px solid #333; border-top: 6px solid var(--blue); border-radius: 50%; width: 50px; height: 50px; animation: spin 1s linear infinite; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
    </style>
</head>
<body>

<div id="loading">
    <div class="spinner"></div>
    <h2 style="margin-top: 20px;">Processando Matemática Mestra...</h2>
</div>

<div class="container">
    <div class="dashboard">
        <div class="card"><h3>💧 Consumo Global (m³)</h3><h1 id="lbl-consumo">0.0</h1></div>
        <div class="card"><h3>🏠 Arrecadação Residencial</h3><h1 id="lbl-arr-res">R$ 0,00</h1></div>
        <div class="card orange"><h3>🏢 Arrecadação Comercial</h3><h1 id="lbl-arr-com">R$ 0,00</h1></div>
        <div class="card red"><h3>🔻 Défice Rateio (Res)</h3><h1 id="lbl-deficit">R$ 0,00</h1></div>
    </div>

    <div class="controls">
        <div style="display: flex; gap: 15px;">
            <div class="input-group">
                <label>Fatura CAESB (Residencial)</label>
                <input type="number" id="val_res" value="214367.61" step="0.01">
            </div>
            <div class="input-group">
                <label style="color:#f39c12;">Fatura CAESB (Comercial)</label>
                <input type="number" id="val_com" value="6004.46" step="0.01" style="color:#f39c12;">
            </div>
        </div>

        <div style="display: flex; gap: 10px;">
            <input type="file" id="file_base" accept=".csv, .xls, .xlsx" style="display:none" onchange="uploadBase()">
            <button class="btn-gold" onclick="document.getElementById('file_base').click()">📁 1. Carregar Dicionário Base</button>
            
            <input type="file" id="file_mes" accept=".csv, .xls, .xlsx" style="display:none" onchange="processarMes()">
            <button class="btn-blue" onclick="document.getElementById('file_mes').click()">⚙️ 2. Processar Mês (REAL CONSUMO)</button>
        </div>
    </div>

    <div class="controls" style="background: #2c3e50;">
        <div class="input-group" style="flex: 1;">
            <label>🔍 Procurar por Medidor, Unidade ou Tipo (Gestor):</label>
            <input type="text" id="searchInput" placeholder="Digite para filtrar a tabela instantaneamente..." onkeyup="filtrarTabela()">
        </div>
        <div style="display: flex; gap: 10px; margin-top: 15px;">
            <button class="btn-green" onclick="exportarExcel()">📊 Exportar Excel</button>
            <button class="btn-red" onclick="exportarPDF()">📄 Emitir Relatório PDF</button>
        </div>
    </div>

    <div class="table-container">
        <table id="dataTable">
            <thead>
                <tr>
                    <th>Unidade</th><th>Tipo</th><th>Medidor</th><th>Data Leit.</th><th>L. Ant.</th><th>L. Atual</th>
                    <th>Consumo</th><th>Fixa Água</th><th>Fixa Esg.</th><th>Var. Água</th><th>Var. Esg.</th>
                    <th>Subtotal</th><th>Rateio</th><th>Taxa</th><th>TOTAL</th>
                </tr>
            </thead>
            <tbody id="tabela-corpo">
                <tr><td colspan="15" style="padding: 30px;">Aguardando dados... Carregue a Base e processe o Mês.</td></tr>
            </tbody>
        </table>
    </div>
</div>

<script>
    let dadosGlobais = []; // Guarda os dados na memória para exportar

    function toggleLoading(show) { document.getElementById('loading').style.display = show ? 'flex' : 'none'; }

    async function uploadBase() {
        const file = document.getElementById('file_base').files[0];
        if(!file) return;
        toggleLoading(true);
        
        let formData = new FormData();
        formData.append("file", file);
        
        try {
            let req = await fetch('/upload_base', { method: "POST", body: formData });
            let res = await req.json();
            toggleLoading(false);
            if(res.error) alert("Erro: " + res.error);
            else alert("✅ " + res.message);
        } catch(e) { toggleLoading(false); alert("Falha na comunicação com o servidor."); }
    }

    async function processarMes() {
        const file = document.getElementById('file_mes').files[0];
        if(!file) return;
        toggleLoading(true);

        let formData = new FormData();
        formData.append("file", file);
        formData.append("caesb_res", document.getElementById('val_res').value);
        formData.append("caesb_com", document.getElementById('val_com').value);

        try {
            let req = await fetch('/processar', { method: "POST", body: formData });
            let res = await req.json();
            toggleLoading(false);
            
            if(res.error) return alert("Erro: " + res.error);

            dadosGlobais = res.data; // Salva para o Excel e PDF
            
            // Atualiza Dashboard
            document.getElementById('lbl-consumo').innerText = res.resumo.consumo_global.toFixed(1);
            document.getElementById('lbl-arr-res').innerText = "R$ " + res.resumo.arr_res.toLocaleString('pt-BR', {minimumFractionDigits: 2});
            document.getElementById('lbl-arr-com').innerText = "R$ " + res.resumo.arr_com.toLocaleString('pt-BR', {minimumFractionDigits: 2});
            document.getElementById('lbl-deficit').innerText = "R$ " + res.resumo.deficit.toLocaleString('pt-BR', {minimumFractionDigits: 2});

            renderTable(dadosGlobais);
            alert("🚀 Cálculos efetuados com Sucesso Mestre!");
            
        } catch(e) { toggleLoading(false); alert("Falha na comunicação com o servidor."); }
    }

    function renderTable(dados) {
        let tbody = document.getElementById('tabela-corpo');
        tbody.innerHTML = "";
        dados.forEach(r => {
            let tipoClass = r.TIPO === 'COMERCIAL' ? 'type-com' : 'type-res';
            let tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${r.UNIDADE}</td>
                <td class="${tipoClass}">${r.TIPO === 'COMERCIAL' ? '🏢 Com' : '🏠 Res'}</td>
                <td>${r.MEDIDOR_VISUAL}</td>
                <td>${r.DATA_LEITURA || 'N/A'}</td>
                <td>${r.L_ANTERIOR.toFixed(0)}</td>
                <td>${r.VAL_ATU.toFixed(0)}</td>
                <td><strong style="color:#2ecc71;">${r.CONSUMO.toFixed(3)}</strong></td>
                <td>${r.FIXA_AGUA > 0 ? r.FIXA_AGUA.toFixed(2) : '-'}</td>
                <td>${r.FIXA_ESGOTO > 0 ? r.FIXA_ESGOTO.toFixed(2) : '-'}</td>
                <td>${r.V_AGUA.toFixed(2)}</td>
                <td>${r.V_ESGOTO > 0 ? r.V_ESGOTO.toFixed(2) : '-'}</td>
                <td>${r.SUBTOTAL.toFixed(2)}</td>
                <td>${r.VAL_RATEIO.toFixed(2)}</td>
                <td>${r.TAXA.toFixed(2)}</td>
                <td><strong style="color:var(--gold);">R$ ${r.TOTAL.toFixed(2)}</strong></td>
            `;
            tbody.appendChild(tr);
        });
    }

    function filtrarTabela() {
        let input = document.getElementById("searchInput").value.toUpperCase();
        let table = document.getElementById("dataTable");
        let tr = table.getElementsByTagName("tr");

        for (let i = 1; i < tr.length; i++) {
            let rowText = tr[i].innerText.toUpperCase();
            tr[i].style.display = rowText.includes(input) ? "" : "none";
        }
    }

    function exportarExcel() {
        if(dadosGlobais.length === 0) return alert("Processe os dados primeiro!");
        let ws = XLSX.utils.table_to_sheet(document.getElementById('dataTable'));
        let wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, "Faturação");
        XLSX.writeFile(wb, "Relatorio_Faturacao_Brisas.xlsx");
    }

    function exportarPDF() {
        if(dadosGlobais.length === 0) return alert("Processe os dados primeiro!");
        const { jsPDF } = window.jspdf;
        const doc = new jsPDF('l', 'mm', 'a4'); // 'l' = Paisagem
        
        doc.setFontSize(18);
        doc.text("RELATORIO GERENCIAL - BRISAS DO LAGO", 14, 15);
        
        doc.setFontSize(10);
        let date = new Date().toLocaleString();
        doc.text("Gerado em: " + date, 14, 22);

        // Prepara dados filtrados visíveis na tabela
        let table = document.getElementById("dataTable");
        doc.autoTable({
            html: table,
            startY: 30,
            theme: 'grid',
            headStyles: { fillColor: [52, 73, 94] },
            styles: { fontSize: 8, cellPadding: 2 }
        });
        
        doc.save("Relatorio_Gerencial_Brisas.pdf");
    }
</script>
</body>
</html>
"""

# --- ROTAS WEB DA APLICAÇÃO ---
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/upload_base', methods=['POST'])
def upload_base():
    if 'file' not in request.files: return jsonify({"error": "Nenhum ficheiro recebido"}), 400
    file = request.files['file']
    try:
        df = MotorFaturamento.ler_ficheiro_memoria(file.read(), file.filename)
        df.columns = [MotorFaturamento.limpa_nome_coluna(c) for c in df.columns]
        df = df.loc[:, ~df.columns.duplicated()].copy()

        col_uni = next((c for c in df.columns if 'UNIDADE' in c or 'LOCAL' in c), None)
        col_med = next((c for c in df.columns if 'MEDIDOR' in c), None)
        col_ind = next((c for c in df.columns if 'INDICE' in c or 'LEITURA' in c), None)

        if not col_uni or not col_med: return jsonify({"error": "Ficheiro Inválido: Falta Coluna Unidade/Medidor"}), 400

        MedidorBase.query.delete() # Zera o banco para atualizar
        adicionados = 0
        for _, r in df.iterrows():
            dna = MotorFaturamento.limpar_id_medidor(r[col_med])
            if dna == "": continue
            
            if not MedidorBase.query.filter_by(medidor_dna=dna).first():
                novo_medidor = MedidorBase(
                    unidade=r[col_uni],
                    tipo=MotorFaturamento.classificar_tipo(r[col_uni]),
                    medidor_visual=str(r[col_med]),
                    medidor_dna=dna,
                    l_anterior=MotorFaturamento.limpar_valor_leitura(r[col_ind]) if col_ind else 0.0
                )
                db.session.add(novo_medidor)
                adicionados += 1
        db.session.commit()
        return jsonify({"message": f"Dicionário Mestre atualizado. {adicionados} equipamentos lidos."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/processar', methods=['POST'])
def processar():
    if 'file' not in request.files: return jsonify({"error": "REAL CONSUMO não recebido"}), 400
    file = request.files['file']
    val_res = float(request.form.get('caesb_res', 0))
    val_com = float(request.form.get('caesb_com', 0))

    try:
        medidores = MedidorBase.query.all()
        if not medidores: return jsonify({"error": "Importe o Dicionário Base primeiro!"}), 400
        
        df_cad = pd.DataFrame([{
            'UNIDADE': m.unidade, 'TIPO': m.tipo, 'MEDIDOR_VISUAL': m.medidor_visual,
            'MEDIDOR_DNA': m.medidor_dna, 'FRACAO': m.fracao, 'TAXA': m.taxa, 'L_ANTERIOR': m.l_anterior
        } for m in medidores])

        df_mes = MotorFaturamento.ler_ficheiro_memoria(file.read(), file.filename)
        df_mes.columns = [MotorFaturamento.limpa_nome_coluna(c) for c in df_mes.columns]
        
        col_id = next((c for c in df_mes.columns if 'IDMEDIDOR' in c or 'MEDIDOR' in c), None)
        col_new = next((c for c in df_mes.columns if ('LEITURA' in c or 'ATU' in c or 'NOVA' in c) and 'DATA' not in c), None)
        col_cons = next((c for c in df_mes.columns if 'CONSUMO' in c), None)
        col_data = next((c for c in df_mes.columns if 'DATA' in c), None)

        if not col_id or not col_new: return jsonify({"error": "Ficheiro sem colunas de Medidor/Leitura!"}), 400

        df_mes['MEDIDOR_DNA'] = df_mes[col_id].apply(MotorFaturamento.limpar_id_medidor)
        df_mes = df_mes.drop_duplicates(subset=['MEDIDOR_DNA'])
        df_mes['VAL_ATU'] = df_mes[col_new].apply(MotorFaturamento.limpar_valor_leitura)
        df_mes['CONS_SISTEMA'] = df_mes[col_cons].apply(MotorFaturamento.limpar_valor_leitura) if col_cons else None
        df_mes['DATA_LEITURA'] = df_mes[col_data] if col_data else "N/A"

        df = pd.merge(df_cad, df_mes[['MEDIDOR_DNA', 'VAL_ATU', 'CONS_SISTEMA', 'DATA_LEITURA']], on='MEDIDOR_DNA', how='left')
        
        if df['VAL_ATU'].isna().all(): return jsonify({"error": "Zero Cruzamentos! Ficheiro de consumo errado."}), 400

        df['VAL_ATU'] = df['VAL_ATU'].fillna(df['L_ANTERIOR'])
        df['CONSUMO'] = df['CONS_SISTEMA'].fillna((df['VAL_ATU'] - df['L_ANTERIOR']).clip(lower=0)).clip(lower=0)
        df['DATA_LEITURA'] = df['DATA_LEITURA'].fillna("N/A")

        df['FIXA_AGUA'] = 0.0; df['FIXA_ESGOTO'] = 0.0; df['V_AGUA'] = 0.0; df['V_ESGOTO'] = 0.0; df['SUBTOTAL'] = 0.0
        mask_res = df['TIPO'] == 'RESIDENCIAL'
        mask_com = df['TIPO'] == 'COMERCIAL'

        # ==========================================
        # MATEMÁTICA RESIDENCIAL OFICIAL
        # ==========================================
        primeiros_hidros = mask_res & ~df.duplicated(subset=['UNIDADE'])
        df.loc[primeiros_hidros, 'FIXA_AGUA'] = 29.34
        df.loc[primeiros_hidros, 'FIXA_ESGOTO'] = 29.34
        df.loc[mask_res, 'V_AGUA'] = df.loc[mask_res, 'CONSUMO'].apply(MotorFaturamento.calcular_caesb_cascata)
        df.loc[mask_res, 'V_ESGOTO'] = df.loc[mask_res, 'V_AGUA']
        df.loc[mask_res, 'SUBTOTAL'] = df['FIXA_AGUA'] + df['FIXA_ESGOTO'] + df['V_AGUA'] + df['V_ESGOTO']

        # ==========================================
        # MATEMÁTICA COMERCIAL RATEADA
        # ==========================================
        total_m3_com = df.loc[mask_com, 'CONSUMO'].sum()
        preco_m3_com = val_com / total_m3_com if total_m3_com > 0 else 0
        df.loc[mask_com, 'V_AGUA'] = df.loc[mask_com, 'CONSUMO'] * preco_m3_com
        df.loc[mask_com, 'SUBTOTAL'] = df.loc[mask_com, 'V_AGUA']

        # ==========================================
        # RATEIO RESIDENCIAL
        # ==========================================
        deficit = val_res - df.loc[mask_res, 'SUBTOTAL'].sum()
        df['VAL_RATEIO'] = (deficit * (df['FRACAO'] / 100)).clip(lower=0)
        df['TOTAL'] = df['SUBTOTAL'] + df['VAL_RATEIO'] + df['TAXA']

        # Retorna os Resultados para montar a Web
        return jsonify({
            "data": df.to_dict(orient='records'),
            "resumo": {
                "consumo_global": float(df['CONSUMO'].sum()),
                "arr_res": float(df.loc[mask_res, 'SUBTOTAL'].sum()),
                "arr_com": float(df.loc[mask_com, 'SUBTOTAL'].sum()),
                "deficit": float(max(0, deficit))
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Ligação direta para rodar no Railway ou Local
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, port=port, host='0.0.0.0')
