import os
import io
import re
import csv
import json
import unicodedata
import pandas as pd
from flask import Flask, render_template_string, request, jsonify
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

# --- CONFIGURAÇÃO DO BANCO DE DADOS (RAILWAY POSTGRES OU LOCAL) ---
db_url = os.getenv("DATABASE_URL", "sqlite:///brisas_enterprise.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
elif db_url.startswith("postgresql://") and not db_url.startswith("postgresql+psycopg://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- MODELOS DO BANCO DE DADOS ---
class MedidorBaseV3(db.Model):
    __tablename__ = 'medidor_base_v3'
    id = db.Column(db.Integer, primary_key=True)
    ordem = db.Column(db.Integer, default=0)
    bloco = db.Column(db.String(20), default="")
    andar = db.Column(db.String(20), default="")
    unidade = db.Column(db.String(100))          # Nome agrupador (ex: A - S1 - 001, KITCLEAN)
    unidade_raw = db.Column(db.String(100))      # Nome original (ex: KITCLEAN 1)
    tipo = db.Column(db.String(50))              # RESIDENCIAL, EMPORIO, COMERCIAL, COMERCIAL_OMO, AREA_COMUM, SEM_MEDIDOR
    hidrometro = db.Column(db.String(100), default="")
    medidor_visual = db.Column(db.String(100))
    medidor_dna = db.Column(db.String(100), unique=True)
    fracao = db.Column(db.Float, default=0.0)
    taxa = db.Column(db.Float, default=7.90)
    l_anterior = db.Column(db.Float, default=0.0)
    data_anterior = db.Column(db.String(50), default="04/09/2026")

class EstadoCalculoV3(db.Model):
    __tablename__ = 'estado_calculo_v3'
    id = db.Column(db.Integer, primary_key=True)
    chave = db.Column(db.String(50), unique=True) # 'mizar' ou 'walkby'
    dados_json = db.Column(db.Text)

with app.app_context():
    db.create_all()

# --- MOTOR MATEMÁTICO E REGRAS DE NEGÓCIO BRISAS DO LAGO ---
class MotorFaturamento:
    AREAS_COMUNS_KEYS = [
        'ACADEMIA', 'JACUZZI', 'BANHEIRO', 'CONDOMINIO', 'LAVA PE', 'LIXEIRA',
        'ROUPARIA', 'SALAO GOURMET', 'SAUNA', 'SPAS', 'TANQUES LAVANDERIA EXCELE',
        'TORNEIRA', 'ESPACO PET', 'GARAGEM'
    ]

    @staticmethod
    def remover_acentos(txt):
        if not txt: return ""
        u = str(txt).upper().strip()
        return ''.join(c for c in unicodedata.normalize('NFKD', u) if not unicodedata.combining(c))

    @staticmethod
    def normalizar_unidade_grupo(unidade_raw, bloco="", andar=""):
        u_clean = MotorFaturamento.remover_acentos(unidade_raw)
        if 'KITCLEAN' in u_clean or 'KIT CLEAN' in u_clean:
            return 'KITCLEAN'
        if 'BLUMA' in u_clean or 'SALAO BELEZA' in u_clean:
            return 'SALÃO BELEZA BLUMA'
        if 'LAVANDERIA OMO' in u_clean:
            return 'LAVANDERIA OMO'
        if 'EMPORIO' in u_clean:
            return 'EMPÓRIO'
        if 'RESTAURANTE' in u_clean:
            return 'RESTAURANTE'
        if 'CYBER' in u_clean:
            return 'CYBER CAFÉ'
        if bloco and andar and re.match(r'^\d+$', u_clean):
            return f"{bloco.strip()} - {andar.strip()} - {u_clean.zfill(3)}"
        # Padroniza A - 101 para A - 1º - 101 se vier do TXT
        m = re.match(r'^([A-E])\s*-\s*([123])\d{2}$', u_clean)
        if m:
            num = u_clean.split('-')[-1].strip()
            return f"{m.group(1)} - {m.group(2)}º - {num}"
        return str(unidade_raw).strip()

    @staticmethod
    def classificar_tipo(unidade_raw, medidor_str="", ordem=0):
        u = MotorFaturamento.remover_acentos(unidade_raw)
        med = str(medidor_str).strip()
        if med == '-' or '251' in u and ('B - 2' in u or 'B-2' in u):
            return 'SEM_MEDIDOR'
        if 'EMPORIO' in u:
            return 'EMPORIO'
        if 'LAVANDERIA OMO' in u:
            return 'COMERCIAL_OMO'
        if any(k in u for k in ['RESTAURANTE', 'CYBER', 'KITCLEAN', 'KIT CLEAN', 'BLUMA', 'SALAO BELEZA']):
            return 'COMERCIAL'
        if ordem >= 1541 or any(k in u for k in MotorFaturamento.AREAS_COMUNS_KEYS):
            return 'AREA_COMUM'
        return 'RESIDENCIAL'

    @staticmethod
    def limpar_id_medidor(val):
        if pd.isna(val) or val is None: return ""
        s = str(val).upper().split('.')[0].strip()
        if s == '-': return '-'
        s = re.sub(r'[^0-9]', '', s)
        return s.lstrip('0')

    @staticmethod
    def calcular_caesb_cascata(m3):
        """Tabela Oficial ADASA nº 70/2026 (01/06/2026 a 31/05/2027)"""
        if m3 <= 0: return 0.0
        f1 = 7 * 4.30    # 30.10
        f2 = 6 * 5.15    # 30.90 -> Acum: 61.00
        f3 = 7 * 10.21   # 71.47 -> Acum: 132.47
        f4 = 10 * 14.81  # 148.10 -> Acum: 280.57
        f5 = 15 * 22.22  # 333.30 -> Acum: 613.87

        if m3 <= 7: return m3 * 4.30
        elif m3 <= 13: return f1 + ((m3 - 7) * 5.15)
        elif m3 <= 20: return f1 + f2 + ((m3 - 13) * 10.21)
        elif m3 <= 30: return f1 + f2 + f3 + ((m3 - 20) * 14.81)
        elif m3 <= 45: return f1 + f2 + f3 + f4 + ((m3 - 30) * 22.22)
        else: return f1 + f2 + f3 + f4 + f5 + ((m3 - 45) * 28.88)

    @staticmethod
    def limpar_num(val, default=0.0):
        if pd.isna(val) or val is None: return default
        s = str(val).replace('"', '').replace("'", '').replace('R$', '').strip()
        if s in ['', '-', 'NAN', 'NONE']: return default
        if ',' in s and '.' in s:
            if s.find('.') < s.find(','):
                s = s.replace('.', '').replace(',', '.')
            else:
                s = s.replace(',', '')
        elif ',' in s:
            s = s.replace(',', '.')
        try: return float(s)
        except: return default

    @staticmethod
    def limpa_nome_coluna(nome):
        s = MotorFaturamento.remover_acentos(nome)
        return re.sub(r'[^A-Z0-9_]', '', s)

    @staticmethod
    def ler_planilha_ou_txt(file_bytes, filename):
        fname = filename.lower()
        if fname.endswith(('.xlsx', '.xls')):
            xls = pd.ExcelFile(io.BytesIO(file_bytes))
            # Caso 1: Planilha Oficial com aba COBRANÇA (WALK-BY ou MIZAR oficial)
            aba_cob = next((s for s in xls.sheet_names if 'COBRAN' in MotorFaturamento.remover_acentos(s) and 'COMERCIAL' not in MotorFaturamento.remover_acentos(s) and 'MODELO' not in MotorFaturamento.remover_acentos(s)), None)
            if aba_cob:
                df_raw = pd.read_excel(xls, sheet_name=aba_cob, header=None)
                if len(df_raw) > 100 and df_raw.shape[1] >= 21:
                    return {'tipo_arquivo': 'PLANILHA_MESTRA_COBRANCA', 'df': df_raw}
            # Caso 2: Planilha Consolidada com walk-by unds e walk-by cobrança
            aba_unds = next((s for s in xls.sheet_names if 'UNDS' in MotorFaturamento.remover_acentos(s)), None)
            if aba_unds:
                df_u = pd.read_excel(xls, sheet_name=aba_unds, header=None)
                return {'tipo_arquivo': 'PLANILHA_UNDS', 'df': df_u}
            # Caso 3: Excel comum (ex: brisas.xlsx)
            df = pd.read_excel(xls, dtype=str)
            return {'tipo_arquivo': 'TABELA_SIMPLES', 'df': df}

        # Leitura de arquivos de texto / CSV / TSV (Walkby.txt ou Mizar.csv)
        texto = ""
        for enc in ['utf-8-sig', 'utf-8', 'latin1', 'cp1252', 'utf-16']:
            try:
                texto = file_bytes.decode(enc)
                if len(texto) > 20: break
            except: continue

        if '<table' in texto.lower():
            dfs = pd.read_html(io.StringIO(texto), decimal=',', thousands='.')
            return {'tipo_arquivo': 'TABELA_SIMPLES', 'df': max(dfs, key=len).astype(str)}

        primeira_linha = texto.splitlines()[0] if texto.splitlines() else ""
        sep = '\t' if primeira_linha.count('\t') > primeira_linha.count(';') else ';'
        if primeira_linha.count(',') > max(primeira_linha.count('\t'), primeira_linha.count(';')):
            sep = ','

        leitor = csv.reader(io.StringIO(texto), delimiter=sep, quotechar='"')
        linhas = [l for l in leitor if len(l) > 0]
        max_cols = max(len(l) for l in linhas)
        linhas_pad = [l + [''] * (max_cols - len(l)) for l in linhas]
        cabecalho = [str(h).strip() if str(h).strip() != '' else f"COL_{i}" for i, h in enumerate(linhas_pad[0])]
        df = pd.DataFrame(linhas_pad[1:], columns=cabecalho)
        return {'tipo_arquivo': 'TABELA_SIMPLES', 'df': df}

    @staticmethod
    def executar_fechamento(df_mes, modo_leitura, val_caesb_res, val_caesb_com, m3_caesb_res=5248, m3_caesb_com=162):
        medidores_db = MedidorBaseV3.query.order_by(MedidorBaseV3.ordem, MedidorBaseV3.id).all()
        if not medidores_db:
            raise ValueError("Dicionário Base vazio! Carregue o Passo 1 primeiro.")

        df_cad = pd.DataFrame([{
            'ORDEM': m.ordem, 'BLOCO': m.bloco, 'ANDAR': m.andar,
            'UNIDADE': m.unidade, 'UNIDADE_RAW': m.unidade_raw, 'TIPO': m.tipo,
            'HIDROMETRO': m.hidrometro, 'MEDIDOR_VISUAL': m.medidor_visual,
            'MEDIDOR_DNA': m.medidor_dna, 'FRACAO': m.fracao,
            'TAXA_BASE': m.taxa, 'L_ANTERIOR_BASE': m.l_anterior,
            'DATA_ANT_BASE': m.data_anterior
        } for m in medidores_db])

        # Identifica colunas no arquivo enviado (Walkby ou Mizar)
        cols_orig = list(df_mes.columns)
        cols_clean = {MotorFaturamento.limpa_nome_coluna(c): c for c in cols_orig}

        # Mapeamento inteligente de colunas
        col_med = next((cols_clean[k] for k in cols_clean if k in ['NDOMEDIDOR', 'N_DO_MEDIDOR', 'ID_MEDIDOR', 'IDMEDIDOR', 'MEDIDOR']), None)
        col_atu = next((cols_clean[k] for k in cols_clean if k in ['INDICE', 'LEITURA', 'LEITURA_ATU', 'LEITURAATU']), None)
        col_ant = next((cols_clean[k] for k in cols_clean if k in ['INDICEANTIGO', 'INDICE_ANTIGO', 'LEITURAANTERIOR']), None)
        col_data = next((cols_clean[k] for k in cols_clean if k in ['DATA_LEVANT', 'DATALEVANT', 'DATA_LEITURA', 'DATALEITURA', 'DATA_LEITURA_ATU']), None)

        # Alarmes do Walk-by
        col_alm_imp = next((cols_clean[k] for k in cols_clean if 'IMPOSSIVEL' in k), None)
        col_alm_desm = next((cols_clean[k] for k in cols_clean if 'DESMONTAGEM' in k or 'CORTE' in k), None)
        col_alm_vaz1 = next((cols_clean[k] for k in cols_clean if k == 'HAVAZAMENTO'), None)
        col_alm_vaz2 = next((cols_clean[k] for k in cols_clean if k == 'HOUVEVAZAMENTO'), None)
        col_alm_frd1 = next((cols_clean[k] for k in cols_clean if k == 'HAFRAUDEMAGNETICA'), None)
        col_alm_frd2 = next((cols_clean[k] for k in cols_clean if k == 'HOUVEFRAUDEMAGNETICA'), None)
        col_alm_blq = next((cols_clean[k] for k in cols_clean if 'BLOQUEADO' in k), None)
        col_alm_sub1 = next((cols_clean[k] for k in cols_clean if k == 'HASUBVAZAO'), None)
        col_alm_sub2 = next((cols_clean[k] for k in cols_clean if 'SEMPREHOUVESUBVAZAO' in k), None)
        col_alm_ret = next((cols_clean[k] for k in cols_clean if 'RETORNO' in k), None)

        if not col_med or not col_atu:
            raise ValueError(f"Colunas de Medidor ou Leitura não identificadas. Colunas lidas: {cols_orig[:8]}")

        registros_mes = {}
        for _, row in df_mes.iterrows():
            dna = MotorFaturamento.limpar_id_medidor(row[col_med])
            if not dna or dna == '-': continue
            l_atu = int(round(MotorFaturamento.limpar_num(row[col_atu], 0.0)))
            l_ant = int(round(MotorFaturamento.limpar_num(row[col_ant], -1.0))) if col_ant else -1
            dt_str = str(row[col_data]).strip().split(' ')[0] if col_data and pd.notna(row[col_data]) else "06/10/2026"

            # Extração de Alarmes
            def flag(c):
                if not c or pd.isna(row[c]): return 0
                v = str(row[c]).strip()
                return 1 if v not in ['0', '0.0', '', 'nan', 'None'] else 0

            alm = {
                'impossivel': flag(col_alm_imp),
                'desmontagem': flag(col_alm_desm),
                'vazamento': 1 if (flag(col_alm_vaz1) or flag(col_alm_vaz2)) else 0,
                'fraude': 1 if (flag(col_alm_frd1) or flag(col_alm_frd2)) else 0,
                'bloqueado': flag(col_alm_blq),
                'subvazao': 1 if (flag(col_alm_sub1) or flag(col_alm_sub2)) else 0,
                'retorno': flag(col_alm_ret),
                'sem_transmissao': 0
            }
            # No Mizar, verifica se a data está congelada (mês diferente de 10/2026 ou anterior ao lote)
            if modo_leitura == 'MIZAR' and ('/10/' not in dt_str and '-10-' not in dt_str):
                alm['sem_transmissao'] = 1

            registros_mes[dna] = {
                'l_atu': l_atu,
                'l_ant': l_ant,
                'data_atu': dt_str,
                'alarmes': alm
            }

        # Monta linhas processadas mantendo a ordem oficial
        linhas_proc = []
        for _, cad in df_cad.iterrows():
            dna = cad['MEDIDOR_DNA']
            tipo = cad['TIPO']
            l_ant_final = int(round(float(cad['L_ANTERIOR_BASE'])))
            dt_ant_final = cad['DATA_ANT_BASE']

            if tipo == 'SEM_MEDIDOR' or dna.startswith('SEM_MED'):
                linhas_proc.append({
                    'ORDEM': int(cad['ORDEM']), 'BLOCO': cad['BLOCO'], 'ANDAR': cad['ANDAR'],
                    'UNIDADE': cad['UNIDADE'], 'UNIDADE_RAW': cad['UNIDADE_RAW'], 'TIPO': 'SEM_MEDIDOR',
                    'HIDROMETRO': '-', 'MEDIDOR_VISUAL': '-', 'MEDIDOR_DNA': dna,
                    'DATA_ANT': '-', 'L_ANTERIOR': 0, 'DATA_LEITURA': '-', 'VAL_ATU': 0,
                    'CONSUMO': 0, 'FRACAO': float(cad['FRACAO']), 'TAXA': 0.0,
                    'ALARMES': {'impossivel':0,'desmontagem':0,'vazamento':0,'fraude':0,'bloqueado':0,'subvazao':0,'retorno':0,'sem_transmissao':0}
                })
                continue

            info_m = registros_mes.get(dna, None)
            if info_m:
                if info_m['l_ant'] >= 0 and l_ant_final == 0:
                    l_ant_final = info_m['l_ant']
                l_atu_final = info_m['l_atu']
                dt_atu_final = info_m['data_atu']
                alm_final = info_m['alarmes']
            else:
                l_atu_final = l_ant_final
                dt_atu_final = "N/A"
                alm_final = {'impossivel':0,'desmontagem':0,'vazamento':0,'fraude':0,'bloqueado':0,'subvazao':0,'retorno':0,'sem_transmissao':1}

            # Tratamento da Virada de Relógio (Ex: Medidor 23003778 na D - 139: 999995 -> 2)
            if l_ant_final >= 999900 and l_atu_final < 1000:
                if modo_leitura == 'MIZAR' and l_atu_final == 0:
                    l_atu_exib = 1000000
                else:
                    l_atu_exib = 1000000 + l_atu_final
                consumo = max(0, l_atu_exib - l_ant_final)
            else:
                l_atu_exib = l_atu_final
                consumo = max(0, l_atu_final - l_ant_final)

            linhas_proc.append({
                'ORDEM': int(cad['ORDEM']), 'BLOCO': cad['BLOCO'], 'ANDAR': cad['ANDAR'],
                'UNIDADE': cad['UNIDADE'], 'UNIDADE_RAW': cad['UNIDADE_RAW'], 'TIPO': tipo,
                'HIDROMETRO': cad['HIDROMETRO'], 'MEDIDOR_VISUAL': cad['MEDIDOR_VISUAL'], 'MEDIDOR_DNA': dna,
                'DATA_ANT': dt_ant_final, 'L_ANTERIOR': l_ant_final, 'DATA_LEITURA': dt_atu_final, 'VAL_ATU': l_atu_exib,
                'CONSUMO': consumo, 'FRACAO': float(cad['FRACAO']), 'TAXA': float(cad['TAXA_BASE']),
                'ALARMES': alm_final
            })

        df = pd.DataFrame(linhas_proc)

        # =========================================================================
        # 1. CÁLCULO COMERCIAL (5 Unidades = 4 Lojas Diretas + Lavanderia OMO)
        # =========================================================================
        mask_com_todos = df['TIPO'].isin(['COMERCIAL', 'COMERCIAL_OMO'])
        total_m3_com = float(df.loc[mask_com_todos, 'CONSUMO'].sum())
        preco_m3_com = (val_caesb_com / total_m3_com) if total_m3_com > 0 else 0.0

        df['FIXA_AGUA'] = 0.0
        df['FIXA_ESGOTO'] = 0.0
        df['V_AGUA'] = 0.0
        df['V_ESGOTO'] = 0.0

        df.loc[mask_com_todos, 'V_AGUA'] = df.loc[mask_com_todos, 'CONSUMO'] * preco_m3_com

        # =========================================================================
        # 2. CÁLCULO RESIDENCIAL, EMPÓRIO, UNIDADE 251 E ÁREAS COMUNS
        # =========================================================================
        mask_res_padrao = df['TIPO'] == 'RESIDENCIAL'
        mask_sem_med = df['TIPO'] == 'SEM_MEDIDOR'
        mask_emporio = df['TIPO'] == 'EMPORIO'
        mask_area_comum = df['TIPO'] == 'AREA_COMUM'

        # Tarifa Fixa (R$ 11,62 Água + R$ 11,62 Esgoto) apenas 1x por Unidade Residencial e na 251 (Empório é isento!)
        mask_recebe_fixa = (mask_res_padrao | mask_sem_med) & (~df.duplicated(subset=['UNIDADE']))
        df.loc[mask_recebe_fixa, 'FIXA_AGUA'] = 11.62
        df.loc[mask_recebe_fixa, 'FIXA_ESGOTO'] = 11.62

        # Tarifa Variável em Cascata (Residencial, Empório e 35 Áreas Comuns)
        mask_cascata = mask_res_padrao | mask_emporio | mask_area_comum
        df.loc[mask_cascata, 'V_AGUA'] = df.loc[mask_cascata, 'CONSUMO'].apply(MotorFaturamento.calcular_caesb_cascata)
        df.loc[mask_cascata, 'V_ESGOTO'] = df.loc[mask_cascata, 'V_AGUA']

        # Linha por Linha: Consumo Individual Total
        df['SUBTOTAL_LINHA'] = df['FIXA_AGUA'] + df['FIXA_ESGOTO'] + df['V_AGUA'] + df['V_ESGOTO']

        # =========================================================================
        # 3. FECHAMENTO DO RATEIO POR FRAÇÃO IDEAL (FÓRMULA OFICIAL DA PLANILHA)
        # =========================================================================
        # Bloco das Unidades Pagantes de Rateio: Empório + 750 Apartamentos (inclui B-251)
        mask_bloco_res = df['TIPO'].isin(['RESIDENCIAL', 'EMPORIO', 'SEM_MEDIDOR'])
        # Bloco de Área Comum (36 medidores = Lavanderia OMO + 35 Áreas Comuns)
        mask_bloco_comum = df['TIPO'].isin(['AREA_COMUM', 'COMERCIAL_OMO'])

        consumo_rs_unidades = float(df.loc[mask_bloco_res, 'SUBTOTAL_LINHA'].sum())
        taxa_leitura_unidades = float(df.loc[mask_bloco_res, 'TAXA'].sum())

        # Na planilha oficial (Linha 26 da aba COBRANÇA), o Esgoto da Área Comum (O26) espelha 100% de N26 (que inclui Lavanderia OMO)
        v_agua_comum_total = float(df.loc[mask_bloco_comum, 'V_AGUA'].sum())
        v_esgoto_comum_total = v_agua_comum_total
        consumo_rs_comum = v_agua_comum_total + v_esgoto_comum_total
        taxa_leitura_comum = float(df.loc[mask_bloco_comum, 'TAXA'].sum())

        taxa_leitura_total = taxa_leitura_unidades + taxa_leitura_comum
        despesa_total_condominio = val_caesb_res + taxa_leitura_total
        arrecadacao_unidades_base = consumo_rs_unidades + taxa_leitura_unidades
        diferenca_caesb = (consumo_rs_unidades + consumo_rs_comum) - val_caesb_res

        # Q35: Valor Exato a ser Rateado por Fração Ideal
        valor_a_ratear = despesa_total_condominio - arrecadacao_unidades_base

        # Aplica a Fração Ideal apenas na 1ª linha de cada Unidade Pagante (Empório + 750 Aptos)
        df['FRACAO_UNICA'] = 0.0
        mask_primeira_linha_res = mask_bloco_res & (~df.duplicated(subset=['UNIDADE']))
        df.loc[mask_primeira_linha_res, 'FRACAO_UNICA'] = df.loc[mask_primeira_linha_res, 'FRACAO']

        df['VAL_RATEIO'] = df['FRACAO_UNICA'] * valor_a_ratear

        # Calcula SUBTOTAL Agrupado da Unidade e TOTAL Agrupado da Unidade (para exibição idêntica à planilha)
        somas_unidade = df.groupby('UNIDADE').agg(
            SUBTOTAL_UNIDADE=('SUBTOTAL_LINHA', 'sum'),
            RATEIO_UNIDADE=('VAL_RATEIO', 'sum'),
            TAXA_UNIDADE=('TAXA', 'sum'),
            CONSUMO_UNIDADE=('CONSUMO', 'sum'),
            QTD_MEDIDORES=('MEDIDOR_DNA', lambda x: sum(1 for i in x if not str(i).startswith('SEM_MED')))
        ).reset_index()
        somas_unidade['TOTAL_UNIDADE'] = somas_unidade['SUBTOTAL_UNIDADE'] + somas_unidade['RATEIO_UNIDADE'] + somas_unidade['TAXA_UNIDADE']

        mapa_sub = somas_unidade.set_index('UNIDADE').to_dict(orient='index')

        df['SUBTOTAL'] = 0.0
        df['TOTAL'] = 0.0
        mask_primeira_qualquer = ~df.duplicated(subset=['UNIDADE'])
        for idx, row in df.iterrows():
            u_nome = row['UNIDADE']
            if row['TIPO'] in ['AREA_COMUM', 'COMERCIAL_OMO']:
                df.at[idx, 'SUBTOTAL'] = row['SUBTOTAL_LINHA']
                df.at[idx, 'TOTAL'] = row['SUBTOTAL_LINHA'] + row['TAXA']
            elif mask_primeira_qualquer.loc[idx]:
                df.at[idx, 'SUBTOTAL'] = mapa_sub[u_nome]['SUBTOTAL_UNIDADE']
                df.at[idx, 'TOTAL'] = mapa_sub[u_nome]['TOTAL_UNIDADE']

        # =========================================================================
        # 4. CONSOLIDAÇÃO POR UNIDADE (ABA COBRANÇA / ÂNCORA)
        # =========================================================================
        unidades_consolidadas = []
        ordem_unidades = df.drop_duplicates(subset=['UNIDADE'])[['ORDEM', 'BLOCO', 'ANDAR', 'UNIDADE', 'TIPO', 'FRACAO_UNICA']]
        for _, u_row in ordem_unidades.iterrows():
            if u_row['TIPO'] in ['AREA_COMUM', 'COMERCIAL_OMO']:
                continue
            u_nome = u_row['UNIDADE']
            info_s = mapa_sub[u_nome]
            unidades_consolidadas.append({
                'ORDEM': int(u_row['ORDEM']),
                'BLOCO': u_row['BLOCO'],
                'ANDAR': u_row['ANDAR'],
                'UNIDADE': u_nome,
                'TIPO': u_row['TIPO'],
                'QTD_MED': int(info_s['QTD_MEDIDORES']),
                'FRACAO': float(u_row['FRACAO_UNICA']),
                'CONSUMO': float(info_s['CONSUMO_UNIDADE']),
                'SUBTOTAL': float(info_s['SUBTOTAL_UNIDADE']),
                'VAL_RATEIO': float(info_s['RATEIO_UNIDADE']),
                'TAXA': float(info_s['TAXA_UNIDADE']),
                'TOTAL': float(info_s['TOTAL_UNIDADE'])
            })

        # =========================================================================
        # 5. CONSOLIDAÇÃO DE ALARMES
        # =========================================================================
        lista_alarmes = []
        contagem_alarmes = {
            'bloqueado': 0, 'vazamento': 0, 'subvazao': 0,
            'retorno': 0, 'desmontagem': 0, 'fraude': 0,
            'impossivel': 0, 'sem_transmissao': 0
        }
        for _, r in df.iterrows():
            alm = r['ALARMES']
            ativos = []
            for k, label in [
                ('bloqueado', 'Medidor Bloqueado / Parado'),
                ('vazamento', 'Alerta de Vazamento'),
                ('subvazao', 'Sub-vazão Detectada'),
                ('retorno', 'Fluxo Reverso / Retorno'),
                ('desmontagem', 'Desmontagem / Manipulação'),
                ('fraude', 'Fraude Magnética'),
                ('impossivel', 'Leitura Impossível'),
                ('sem_transmissao', 'Sem Transmissão Recente (Congelado)')
            ]:
                if alm.get(k, 0) == 1:
                    contagem_alarmes[k] += 1
                    ativos.append(label)
            if ativos:
                lista_alarmes.append({
                    'UNIDADE': r['UNIDADE'],
                    'TIPO': r['TIPO'],
                    'HIDROMETRO': r['HIDROMETRO'],
                    'MEDIDOR': r['MEDIDOR_VISUAL'],
                    'DATA_LEITURA': r['DATA_LEITURA'],
                    'L_ANTERIOR': r['L_ANTERIOR'],
                    'VAL_ATU': r['VAL_ATU'],
                    'CONSUMO': r['CONSUMO'],
                    'ALERTAS': " | ".join(ativos)
                })

        # Resumo Executivo Geral (Aba GERAL)
        m3_res_unidades = float(df.loc[mask_bloco_res, 'CONSUMO'].sum())
        m3_area_comum = float(df.loc[mask_bloco_comum, 'CONSUMO'].sum())
        arrecadacao_com_lojas = float(df.loc[df['TIPO'] == 'COMERCIAL', 'SUBTOTAL_LINHA'].sum() + df.loc[df['TIPO'] == 'COMERCIAL', 'TAXA'].sum())
        arrecadacao_com_total_5 = float(df.loc[mask_com_todos, 'SUBTOTAL_LINHA'].sum() + df.loc[mask_com_todos, 'TAXA'].sum())

        resumo = {
            'modo': modo_leitura,
            'fatura_caesb_res': val_caesb_res,
            'm3_caesb_res': m3_caesb_res,
            'fatura_caesb_com': val_caesb_com,
            'm3_caesb_com': m3_caesb_com,
            'consumo_global_m3': m3_res_unidades + m3_area_comum + float(df.loc[df['TIPO'] == 'COMERCIAL', 'CONSUMO'].sum()),
            'm3_unidades_res': m3_res_unidades,
            'm3_area_comum': m3_area_comum,
            'm3_comercial_5unid': total_m3_com,
            'preco_m3_com': preco_m3_com,
            'consumo_rs_unidades': consumo_rs_unidades,
            'taxa_leitura_unidades': taxa_leitura_unidades,
            'arrecadacao_unidades_base': arrecadacao_unidades_base,
            'consumo_rs_comum': consumo_rs_comum,
            'taxa_leitura_comum': taxa_leitura_comum,
            'taxa_leitura_total': taxa_leitura_total,
            'despesa_total_condominio': despesa_total_condominio,
            'diferenca_caesb': diferenca_caesb,
            'status_diferenca': 'Superávit' if diferenca_caesb >= 0 else 'Déficit',
            'valor_a_ratear': valor_a_ratear,
            'arrecadacao_final_res': arrecadacao_unidades_base + valor_a_ratear,
            'arrecadacao_com_5unid': arrecadacao_com_total_5,
            'arrecadacao_com_4lojas': arrecadacao_com_lojas,
            'contagem_alarmes': contagem_alarmes,
            'total_alarmes': len(lista_alarmes)
        }

        return {
            'resumo': resumo,
            'unidades_hidrometros': df.drop(columns=['ALARMES']).to_dict(orient='records'),
            'cobranca_consolidada': unidades_consolidadas,
            'alarmes_lista': lista_alarmes
        }


# ==============================================================================
# INTERFACE WEB ENTERPRISE (HTML5 / CSS3 / JS - LAYOUT FIEL À IMAGEM)
# ==============================================================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Brisas do Lago | Sistema Integrado Mizar & Walk-by</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf-autotable/3.5.28/jspdf.plugin.autotable.min.js"></script>
    <style>
        :root {
            --bg-dark: #09101d;
            --panel-bg: #111c2d;
            --panel-border: #1e2d42;
            --mizar-blue: #1a73e8;
            --mizar-glow: rgba(26, 115, 232, 0.35);
            --walkby-green: #10b981;
            --walkby-glow: rgba(16, 185, 129, 0.35);
            --comp-slate: #6366f1;
            --gold: #d97706;
            --danger: #ef4444;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
        }
        * { box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background: radial-gradient(circle at top, #132036 0%, #070c16 100%);
            color: var(--text-main);
            margin: 0;
            padding: 18px;
            min-height: 100vh;
        }
        .container { max-width: 1560px; margin: auto; }

        /* BARRA SUPERIOR DE MÓDULOS (1. MIZAR | 2. WALK-BY | 3. COMPARATIVO) */
        .top-modules {
            display: grid;
            grid-template-columns: 1fr 1fr 0.75fr;
            gap: 14px;
            margin-bottom: 12px;
        }
        .module-btn {
            background: rgba(17, 28, 45, 0.85);
            border: 1px solid var(--panel-border);
            border-radius: 12px;
            padding: 14px 20px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            cursor: pointer;
            transition: all 0.25s ease;
            color: var(--text-main);
            font-weight: 700;
            font-size: 16px;
            letter-spacing: 0.5px;
        }
        .module-left { display: flex; align-items: center; gap: 12px; }
        .badge-num {
            width: 30px; height: 30px; border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            font-size: 14px; font-weight: 800; background: #1e293b;
        }
        .status-pill {
            font-size: 11px; padding: 4px 10px; border-radius: 20px;
            background: rgba(255,255,255,0.08); color: var(--text-muted);
        }
        .module-btn.active-mizar {
            background: linear-gradient(90deg, rgba(26,115,232,0.35) 0%, rgba(17,28,45,0.9) 100%);
            border-color: var(--mizar-blue);
            box-shadow: 0 0 18px var(--mizar-glow);
        }
        .module-btn.active-mizar .badge-num { background: var(--mizar-blue); }
        .module-btn.active-walkby {
            background: linear-gradient(90deg, rgba(16,185,129,0.35) 0%, rgba(17,28,45,0.9) 100%);
            border-color: var(--walkby-green);
            box-shadow: 0 0 18px var(--walkby-glow);
        }
        .module-btn.active-walkby .badge-num { background: var(--walkby-green); }
        .module-btn.active-comp {
            background: linear-gradient(90deg, rgba(99,102,241,0.35) 0%, rgba(17,28,45,0.9) 100%);
            border-color: var(--comp-slate);
            box-shadow: 0 0 18px rgba(99,102,241,0.35);
        }
        .module-btn.active-comp .badge-num { background: var(--comp-slate); }

        /* SUB-ABAS (GERAL | UNIDADES | COBRANÇA | ALARMES) */
        .sub-nav {
            display: flex;
            gap: 10px;
            margin-bottom: 18px;
            flex-wrap: wrap;
        }
        .sub-tab {
            background: #111c2d;
            border: 1px solid var(--panel-border);
            color: var(--text-muted);
            padding: 10px 22px;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 700;
            font-size: 13px;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: 0.2s;
        }
        .sub-tab:hover { color: white; border-color: #3b82f6; }
        .sub-tab.active-sub-mizar { background: var(--mizar-blue); color: white; border-color: var(--mizar-blue); }
        .sub-tab.active-sub-walkby { background: var(--walkby-green); color: white; border-color: var(--walkby-green); }
        .sub-tab.active-sub-comp { background: var(--comp-slate); color: white; border-color: var(--comp-slate); }

        /* CARDS KPI PRINCIPAIS */
        .kpi-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 15px;
            margin-bottom: 18px;
        }
        .kpi-card {
            background: var(--panel-bg);
            border: 1px solid var(--panel-border);
            border-radius: 12px;
            padding: 18px 20px;
            display: flex;
            align-items: center;
            gap: 16px;
            box-shadow: 0 6px 15px rgba(0,0,0,0.25);
        }
        .kpi-icon {
            width: 52px; height: 52px; border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            font-size: 22px; flex-shrink: 0;
        }
        .icon-blue { background: rgba(26, 115, 232, 0.2); color: #38bdf8; border: 1px solid rgba(56,189,248,0.3); }
        .icon-amber { background: rgba(217, 119, 6, 0.2); color: #fbbf24; border: 1px solid rgba(251,191,36,0.3); }
        .icon-indigo { background: rgba(99, 102, 241, 0.2); color: #818cf8; border: 1px solid rgba(129,140,248,0.3); }
        .icon-red { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(248,113,113,0.3); }
        .kpi-info h4 { margin: 0 0 6px 0; font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.6px; }
        .kpi-info h2 { margin: 0; font-size: 25px; font-weight: 800; }

        /* BARRA DE CONTROLES E UPLOADS */
        .control-panel {
            background: var(--panel-bg);
            border: 1px solid var(--panel-border);
            border-radius: 12px;
            padding: 16px 20px;
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 15px;
            margin-bottom: 16px;
        }
        .inputs-wrap { display: flex; gap: 14px; flex-wrap: wrap; align-items: center; }
        .field-box { display: flex; flex-direction: column; gap: 5px; }
        .field-box label { font-size: 12px; color: var(--text-muted); font-weight: 600; }
        .field-box input {
            background: #09101d; border: 1px solid #2dd4bf40;
            color: white; padding: 10px 14px; border-radius: 8px;
            font-weight: 700; font-size: 14px; width: 175px;
        }
        .btn-group { display: flex; gap: 10px; flex-wrap: wrap; }
        button {
            padding: 11px 18px; border: none; border-radius: 8px;
            font-weight: 700; font-size: 13px; cursor: pointer;
            color: white; display: flex; align-items: center; gap: 8px;
            transition: 0.2s;
        }
        button:hover { filter: brightness(1.15); transform: translateY(-1px); }
        .btn-gold { background: linear-gradient(135deg, #d97706, #b45309); }
        .btn-mizar { background: linear-gradient(135deg, #2563eb, #1d4ed8); }
        .btn-walkby { background: linear-gradient(135deg, #10b981, #059669); }
        .btn-excel { background: linear-gradient(135deg, #16a34a, #15803d); }
        .btn-pdf { background: linear-gradient(135deg, #dc2626, #b91c1c); }

        /* BARRA DE PESQUISA E EXPORTAÇÃO */
        .search-panel {
            background: #16243a;
            border: 1px solid var(--panel-border);
            border-radius: 12px;
            padding: 14px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 15px;
            margin-bottom: 16px;
            flex-wrap: wrap;
        }
        .search-box { flex: 1; min-width: 280px; display: flex; flex-direction: column; gap: 6px; }
        .search-box label { font-size: 13px; font-weight: 700; color: #cbd5e1; }
        .search-box input {
            width: 100%; padding: 11px 15px; border-radius: 8px;
            border: 1px solid #334155; background: #0b1322; color: white; font-size: 14px;
        }

        /* TABELAS E PAINÉIS DE CONTEÚDO */
        .content-box {
            background: var(--panel-bg);
            border: 1px solid var(--panel-border);
            border-radius: 12px;
            padding: 16px;
            overflow-x: auto;
            max-height: 620px;
        }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th {
            background: #1a2942; color: #e2e8f0; padding: 12px 10px;
            position: sticky; top: 0; z-index: 10; text-align: center;
            font-weight: 700; border-bottom: 2px solid #2d4263;
        }
        td { padding: 10px 8px; text-align: center; border-bottom: 1px solid #1e293b; }
        tr:hover { background: rgba(59, 130, 246, 0.08); }

        .tag-res { color: #38bdf8; font-weight: 700; }
        .tag-com { color: #fbbf24; font-weight: 700; }
        .tag-emp { color: #a78bfa; font-weight: 700; }
        .tag-are { color: #34d399; font-weight: 700; }
        .tag-sem { color: #f472b6; font-weight: 700; }

        /* QUADROS EXECUTIVOS DA ABA GERAL */
        .exec-section { margin-bottom: 24px; }
        .exec-title {
            background: linear-gradient(90deg, #1e3a8a, #0f172a);
            padding: 10px 16px; border-radius: 8px; font-weight: 800;
            font-size: 14px; letter-spacing: 0.5px; margin-bottom: 10px;
            border-left: 4px solid #38bdf8;
        }
        .exec-title.orange { background: linear-gradient(90deg, #7c2d12, #0f172a); border-left-color: #fbbf24; }
        .exec-title.green { background: linear-gradient(90deg, #065f46, #0f172a); border-left-color: #34d399; }

        /* OVERLAY DE CARREGAMENTO */
        #loading {
            display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(7, 12, 22, 0.85); z-index: 9999;
            justify-content: center; align-items: center; flex-direction: column;
        }
        .spinner {
            border: 5px solid #1e293b; border-top: 5px solid #38bdf8;
            border-radius: 50%; width: 54px; height: 54px; animation: spin 0.9s linear infinite;
        }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
    </style>
</head>
<body>

<div id="loading">
    <div class="spinner"></div>
    <h3 id="loading-msg" style="margin-top: 18px; color: #38bdf8;">Processando Engenharia CAESB...</h3>
</div>

<div class="container">
    <!-- 1. MÓDULOS PRINCIPAIS -->
    <div class="top-modules">
        <div id="mod-MIZAR" class="module-btn active-mizar" onclick="selecionarModulo('MIZAR')">
            <div class="module-left">
                <span class="badge-num">1</span>
                <span>💧 MIZAR (Telemetria)</span>
            </div>
            <span id="status-mizar" class="status-pill">Aguardando CSV</span>
        </div>
        <div id="mod-WALKBY" class="module-btn" onclick="selecionarModulo('WALKBY')">
            <div class="module-left">
                <span class="badge-num">2</span>
                <span>🚶 WALK-BY (Coleta Antena)</span>
            </div>
            <span id="status-walkby" class="status-pill">Aguardando TXT/XLSX</span>
        </div>
        <div id="mod-COMPARATIVO" class="module-btn" onclick="selecionarModulo('COMPARATIVO')">
            <div class="module-left">
                <span class="badge-num">3</span>
                <span>⚖️ COMPARATIVO</span>
            </div>
            <span class="status-pill">Mizar vs Walk-by</span>
        </div>
    </div>

    <!-- 2. SUB-ABAS DE NAVEGAÇÃO -->
    <div class="sub-nav" id="sub-nav-bar">
        <button id="sub-GERAL" class="sub-tab active-sub-mizar" onclick="selecionarSubAba('GERAL')">🏠 GERAL (Demonstrativo)</button>
        <button id="sub-UNIDADES" class="sub-tab" onclick="selecionarSubAba('UNIDADES')">🏢 UNIDADES (1.576 Hidrômetros)</button>
        <button id="sub-COBRANCA" class="sub-tab" onclick="selecionarSubAba('COBRANCA')">💲 COBRANÇA (Âncora 756 Unids)</button>
        <button id="sub-ALARMES" class="sub-tab" onclick="selecionarSubAba('ALARMES')">🔔 ALARMES & AUDITORIA</button>
    </div>

    <!-- 3. CARDS KPI -->
    <div class="kpi-grid">
        <div class="kpi-card">
            <div class="kpi-icon icon-blue">💧</div>
            <div class="kpi-info">
                <h4 id="kpi1-title">CONSUMO GLOBAL (M³)</h4>
                <h2 id="lbl-consumo">0.0</h2>
            </div>
        </div>
        <div class="kpi-card">
            <div class="kpi-icon icon-amber">🏠</div>
            <div class="kpi-info">
                <h4 id="kpi2-title">ARRECADAÇÃO RESIDENCIAL</h4>
                <h2 id="lbl-arr-res">R$ 0,00</h2>
            </div>
        </div>
        <div class="kpi-card">
            <div class="kpi-icon icon-indigo">🏢</div>
            <div class="kpi-info">
                <h4 id="kpi3-title">ARRECADAÇÃO COMERCIAL</h4>
                <h2 id="lbl-arr-com" style="color:#fbbf24;">R$ 0,00</h2>
            </div>
        </div>
        <div class="kpi-card">
            <div class="kpi-icon icon-red">🔺</div>
            <div class="kpi-info">
                <h4 id="kpi4-title">RATEIO ÁREA COMUM (R$)</h4>
                <h2 id="lbl-deficit" style="color:#f87171;">R$ 0,00</h2>
            </div>
        </div>
    </div>

    <!-- 4. PAINEL DE FATURAS E UPLOADS -->
    <div class="control-panel">
        <div class="inputs-wrap">
            <div class="field-box">
                <label>Fatura CAESB (Residencial R$)</label>
                <input type="number" id="val_res" value="62562.80" step="0.01">
            </div>
            <div class="field-box">
                <label>Consumo CAESB (Res m³)</label>
                <input type="number" id="m3_res" value="5248" step="1" style="width:120px;">
            </div>
            <div class="field-box">
                <label style="color:#fbbf24;">Fatura CAESB (Comercial R$)</label>
                <input type="number" id="val_com" value="6452.54" step="0.01" style="color:#fbbf24; border-color:#fbbf2460;">
            </div>
            <div class="field-box">
                <label style="color:#fbbf24;">Consumo CAESB (Com m³)</label>
                <input type="number" id="m3_com" value="162" step="1" style="width:120px; color:#fbbf24;">
            </div>
        </div>

        <div class="btn-group">
            <input type="file" id="file_base" accept=".xlsx,.xls,.csv" style="display:none" onchange="uploadBase()">
            <button class="btn-gold" onclick="document.getElementById('file_base').click()">📁 1. Carregar Base / Frações</button>

            <input type="file" id="file_mizar" accept=".csv,.txt,.xlsx,.xls" style="display:none" onchange="processarArquivo('MIZAR')">
            <button class="btn-mizar" onclick="document.getElementById('file_mizar').click()">💧 2. Processar MIZAR</button>

            <input type="file" id="file_walkby" accept=".txt,.csv,.xlsx,.xls" style="display:none" onchange="processarArquivo('WALKBY')">
            <button class="btn-walkby" onclick="document.getElementById('file_walkby').click()">🚶 3. Processar WALK-BY</button>
        </div>
    </div>

    <!-- 5. BARRA DE PESQUISA E EXPORTAÇÕES -->
    <div class="search-panel">
        <div class="search-box">
            <label>🔍 Procurar por Medidor, Hidrômetro, Unidade ou Alarme (Filtro Instantâneo):</label>
            <input type="text" id="searchInput" placeholder="Ex: 251, KITCLEAN, EMPORIO, 23712149, Bloqueado..." onkeyup="filtrarTabelaVisivel()">
        </div>
        <div class="btn-group">
            <button class="btn-excel" onclick="exportarExcelCompleto()">📊 Exportar Excel (Todas as Abas)</button>
            <button class="btn-pdf" onclick="exportarPDFExecutivo()">📄 Emitir PDF Gerencial Resumido</button>
        </div>
    </div>

    <!-- 6. ÁREA DINÂMICA DE CONTEÚDO -->
    <div class="content-box" id="main-content">
        <div style="padding: 40px; text-align: center; color: var(--text-muted);">
            <h3>Bem-vindo ao Sistema de Gestão de Água — Condomínio Brisas do Lago</h3>
            <p>1. Clique em <b>Carregar Base / Frações</b> (pode enviar a planilha oficial de Outubro ou brisas.xlsx).<br>
               2. Envie o arquivo <b>MIZAR (.csv)</b> e/ou <b>WALK-BY (.txt)</b> para visualizar todas as abas e o comparativo.</p>
        </div>
    </div>
</div>

<script>
    let estado = {
        moduloAtivo: 'MIZAR',    // 'MIZAR', 'WALKBY', 'COMPARATIVO'
        subAbaAtiva: 'GERAL',    // 'GERAL', 'UNIDADES', 'COBRANCA', 'ALARMES'
        MIZAR: null,
        WALKBY: null
    };

    const fmtBRL = (v) => "R$ " + Number(v || 0).toLocaleString('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    const fmtNum = (v, d=0) => Number(v || 0).toLocaleString('pt-BR', {minimumFractionDigits: d, maximumFractionDigits: d});

    function showLoading(show, msg="Processando Matemática Mestra...") {
        document.getElementById('loading-msg').innerText = msg;
        document.getElementById('loading').style.display = show ? 'flex' : 'none';
    }

    // Carrega dados salvos no banco ao abrir a página
    window.addEventListener('DOMContentLoaded', async () => {
        try {
            let req = await fetch('/estado_inicial');
            let res = await req.json();
            if (res.MIZAR) {
                estado.MIZAR = res.MIZAR;
                document.getElementById('status-mizar').innerText = "✅ Carregado";
                document.getElementById('status-mizar').style.color = "#38bdf8";
            }
            if (res.WALKBY) {
                estado.WALKBY = res.WALKBY;
                document.getElementById('status-walkby').innerText = "✅ Carregado";
                document.getElementById('status-walkby').style.color = "#34d399";
            }
            if (estado.WALKBY && !estado.MIZAR) selecionarModulo('WALKBY');
            else renderizarTudo();
        } catch (e) { console.log(e); }
    });

    function selecionarModulo(mod) {
        estado.moduloAtivo = mod;
        document.getElementById('mod-MIZAR').className = 'module-btn' + (mod === 'MIZAR' ? ' active-mizar' : '');
        document.getElementById('mod-WALKBY').className = 'module-btn' + (mod === 'WALKBY' ? ' active-walkby' : '');
        document.getElementById('mod-COMPARATIVO').className = 'module-btn' + (mod === 'COMPARATIVO' ? ' active-comp' : '');

        // Atualiza estilo das sub-abas
        atualizarBotoesSubAba();
        renderizarTudo();
    }

    function selecionarSubAba(sub) {
        estado.subAbaAtiva = sub;
        atualizarBotoesSubAba();
        renderizarTudo();
    }

    function atualizarBotoesSubAba() {
        const subBar = document.getElementById('sub-nav-bar');
        if (estado.moduloAtivo === 'COMPARATIVO') {
            subBar.style.display = 'none';
            return;
        }
        subBar.style.display = 'flex';
        const cls = estado.moduloAtivo === 'MIZAR' ? 'active-sub-mizar' : 'active-sub-walkby';
        ['GERAL', 'UNIDADES', 'COBRANCA', 'ALARMES'].forEach(s => {
            document.getElementById('sub-' + s).className = 'sub-tab' + (estado.subAbaAtiva === s ? ' ' + cls : '');
        });
    }

    async function uploadBase() {
        const file = document.getElementById('file_base').files[0];
        if (!file) return;
        showLoading(true, "Importando Dicionário Base, Unidade 251 e Frações Ideais...");
        let fd = new FormData();
        fd.append("file", file);
        try {
            let req = await fetch('/upload_base', { method: 'POST', body: fd });
            let res = await req.json();
            showLoading(false);
            if (res.error) alert("❌ Erro: " + res.error);
            else alert("✅ " + res.message);
        } catch (e) {
            showLoading(false);
            alert("Erro na comunicação com o servidor.");
        }
    }

    async function processarArquivo(modo) {
        const inputId = modo === 'MIZAR' ? 'file_mizar' : 'file_walkby';
        const file = document.getElementById(inputId).files[0];
        if (!file) return;

        showLoading(true, `Calculando Fechamento ${modo} (Cascata ADASA + Comercial + Rateio)...`);
        let fd = new FormData();
        fd.append("file", file);
        fd.append("modo", modo);
        fd.append("caesb_res", document.getElementById('val_res').value);
        fd.append("caesb_com", document.getElementById('val_com').value);
        fd.append("m3_res", document.getElementById('m3_res').value);
        fd.append("m3_com", document.getElementById('m3_com').value);

        try {
            let req = await fetch('/processar', { method: 'POST', body: fd });
            let res = await req.json();
            showLoading(false);
            if (res.error) return alert("❌ Erro: " + res.error);

            estado[modo] = res;
            if (modo === 'MIZAR') {
                document.getElementById('status-mizar').innerText = "✅ Processado";
                document.getElementById('status-mizar').style.color = "#38bdf8";
            } else {
                document.getElementById('status-walkby').innerText = "✅ Processado";
                document.getElementById('status-walkby').style.color = "#34d399";
            }
            selecionarModulo(modo);
        } catch (e) {
            showLoading(false);
            alert("Falha ao processar arquivo no servidor.");
        }
    }

    function renderizarTudo() {
        const box = document.getElementById('main-content');
        if (estado.moduloAtivo === 'COMPARATIVO') {
            renderizarComparativo(box);
            return;
        }

        const dadosModo = estado[estado.moduloAtivo];
        if (!dadosModo) {
            document.getElementById('lbl-consumo').innerText = "0.0";
            document.getElementById('lbl-arr-res').innerText = "R$ 0,00";
            document.getElementById('lbl-arr-com').innerText = "R$ 0,00";
            document.getElementById('lbl-deficit').innerText = "R$ 0,00";
            box.innerHTML = `<div style="padding:45px; text-align:center; color:#94a3b8;">
                <h3>Módulo ${estado.moduloAtivo} ainda não carregado</h3>
                <p>Clique no botão <b>Processar ${estado.moduloAtivo}</b> acima e selecione o arquivo correspondente.</p>
            </div>`;
            return;
        }

        const R = dadosModo.resumo;
        document.getElementById('kpi1-title').innerText = `CONSUMO MEDIDO UNIDS (${estado.moduloAtivo})`;
        document.getElementById('kpi2-title').innerText = `ARRECADAÇÃO UNIDADES (S/ RATEIO)`;
        document.getElementById('kpi3-title').innerText = `FATURA COMERCIAL (${fmtNum(R.m3_comercial_5unid)} M³)`;
        document.getElementById('kpi4-title').innerText = `VALOR A RATEAR POR FRAÇÃO`;

        document.getElementById('lbl-consumo').innerText = fmtNum(R.m3_unidades_res) + " m³";
        document.getElementById('lbl-arr-res').innerText = fmtBRL(R.arrecadacao_unidades_base);
        document.getElementById('lbl-arr-com').innerText = fmtBRL(R.fatura_caesb_com);
        document.getElementById('lbl-deficit').innerText = fmtBRL(R.valor_a_ratear);

        if (estado.subAbaAtiva === 'GERAL') renderizarAbaGeral(box, dadosModo);
        else if (estado.subAbaAtiva === 'UNIDADES') renderizarAbaUnidades(box, dadosModo);
        else if (estado.subAbaAtiva === 'COBRANCA') renderizarAbaCobranca(box, dadosModo);
        else if (estado.subAbaAtiva === 'ALARMES') renderizarAbaAlarmes(box, dadosModo);
    }

    function renderizarAbaGeral(box, dadosModo) {
        const R = dadosModo.resumo;
        const comRows = dadosModo.unidades_hidrometros.filter(x => x.TIPO === 'COMERCIAL' || x.TIPO === 'COMERCIAL_OMO');

        let htmlCom = comRows.map(c => `
            <tr>
                <td style="text-align:left; font-weight:700;">${c.UNIDADE_RAW}</td>
                <td>${c.UNIDADE}</td>
                <td>${c.HIDROMETRO}</td>
                <td>${c.MEDIDOR_VISUAL}</td>
                <td>${fmtNum(c.L_ANTERIOR)}</td>
                <td>${fmtNum(c.VAL_ATU)}</td>
                <td style="font-weight:800; color:#34d399;">${fmtNum(c.CONSUMO)} m³</td>
                <td>${fmtBRL(c.V_AGUA)}</td>
                <td>${fmtBRL(c.TAXA)}</td>
                <td style="font-weight:800; color:#fbbf24;">${fmtBRL(c.V_AGUA + c.TAXA)}</td>
                <td>${c.TIPO === 'COMERCIAL_OMO' ? '<span class="tag-are">Rateio Área Comum</span>' : '<span class="tag-com">Cobrança Proprietário</span>'}</td>
            </tr>
        `).join('');

        box.innerHTML = `
            <div class="exec-section">
                <div class="exec-title">1. RESUMO GERAL — UNIDADES RESIDENCIAIS E ÁREAS COMUNS (${R.modo})</div>
                <table class="filterable-table">
                    <thead>
                        <tr>
                            <th>Consumo Unidades (m³)</th>
                            <th>Consumo Unidades (R$)</th>
                            <th>Tx. Leitura Unidades</th>
                            <th>Consumo Área Comum + OMO (R$)</th>
                            <th>Tx. Leitura Área Comum</th>
                            <th>Fatura CAESB Geral</th>
                            <th>Despesa Total (CAESB + Leit.)</th>
                            <th>Arrecadação Unidades</th>
                            <th>Status CAESB</th>
                            <th>VALOR A RATEAR (FRAÇÃO)</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr style="font-size:14px; font-weight:700;">
                            <td style="color:#38bdf8;">${fmtNum(R.m3_unidades_res)} m³</td>
                            <td>${fmtBRL(R.consumo_rs_unidades)}</td>
                            <td>${fmtBRL(R.taxa_leitura_unidades)}</td>
                            <td>${fmtBRL(R.consumo_rs_comum)}</td>
                            <td>${fmtBRL(R.taxa_leitura_comum)}</td>
                            <td>${fmtBRL(R.fatura_caesb_res)}</td>
                            <td style="color:#fbbf24;">${fmtBRL(R.despesa_total_condominio)}</td>
                            <td style="color:#34d399;">${fmtBRL(R.arrecadacao_unidades_base)}</td>
                            <td>${R.status_diferenca} (${fmtBRL(R.diferenca_caesb)})</td>
                            <td style="color:#f87171; font-size:16px;">${fmtBRL(R.valor_a_ratear)}</td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <div class="exec-section">
                <div class="exec-title orange">2. RESUMO E MEMÓRIA DE CÁLCULO — UNIDADES COMERCIAIS (5 UNIDADES / 7 MEDIDORES)</div>
                <table style="margin-bottom:12px;">
                    <thead>
                        <tr>
                            <th>Fatura CAESB Comercial</th>
                            <th>Consumo Externo CAESB</th>
                            <th>Soma Consumo 5 Comércios</th>
                            <th>Valor do m³ Comercial Calculado</th>
                            <th>Taxa de Leitura (7 Medidores)</th>
                            <th>Despesa Total Comercial</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr style="font-weight:700; font-size:14px;">
                            <td>${fmtBRL(R.fatura_caesb_com)}</td>
                            <td>${fmtNum(R.m3_caesb_com)} m³</td>
                            <td style="color:#34d399;">${fmtNum(R.m3_comercial_5unid)} m³</td>
                            <td style="color:#fbbf24; font-size:15px;">${fmtBRL(R.preco_m3_com)} / m³</td>
                            <td>${fmtBRL(55.30)}</td>
                            <td style="color:#fbbf24;">${fmtBRL(R.arrecadacao_com_5unid)}</td>
                        </tr>
                    </tbody>
                </table>

                <table class="filterable-table">
                    <thead>
                        <tr>
                            <th>Medidor / Loja</th><th>Grupo</th><th>Hidrômetro</th><th>Medidor</th>
                            <th>L. Ant.</th><th>L. Atual</th><th>Consumo</th><th>Tarifa Variável</th>
                            <th>Tx. Leitura</th><th>Total Linha</th><th>Destinação Contábil</th>
                        </tr>
                    </thead>
                    <tbody>${htmlCom}</tbody>
                </table>
            </div>
        `;
    }

    function badgeTipo(t) {
        if (t === 'RESIDENCIAL') return '<span class="tag-res">🏠 Res</span>';
        if (t === 'COMERCIAL' || t === 'COMERCIAL_OMO') return '<span class="tag-com">🏢 Com</span>';
        if (t === 'EMPORIO') return '<span class="tag-emp">🏪 Empório</span>';
        if (t === 'SEM_MEDIDOR') return '<span class="tag-sem">📌 Fixa (251)</span>';
        return '<span class="tag-are">🌳 Área Comum</span>';
    }

    function renderizarAbaUnidades(box, dadosModo) {
        let rows = dadosModo.unidades_hidrometros.map(r => `
            <tr>
                <td style="font-weight:700;">${r.UNIDADE}</td>
                <td>${badgeTipo(r.TIPO)}</td>
                <td>${r.HIDROMETRO}</td>
                <td>${r.MEDIDOR_VISUAL}</td>
                <td>${r.DATA_LEITURA}</td>
                <td>${r.TIPO === 'SEM_MEDIDOR' ? '-' : fmtNum(r.L_ANTERIOR)}</td>
                <td>${r.TIPO === 'SEM_MEDIDOR' ? '-' : fmtNum(r.VAL_ATU)}</td>
                <td style="font-weight:800; color:#34d399;">${r.TIPO === 'SEM_MEDIDOR' ? '-' : fmtNum(r.CONSUMO)}</td>
                <td>${r.FIXA_AGUA > 0 ? fmtBRL(r.FIXA_AGUA) : '-'}</td>
                <td>${r.FIXA_ESGOTO > 0 ? fmtBRL(r.FIXA_ESGOTO) : '-'}</td>
                <td>${r.V_AGUA > 0 ? fmtBRL(r.V_AGUA) : 'R$ 0,00'}</td>
                <td>${r.V_ESGOTO > 0 ? fmtBRL(r.V_ESGOTO) : '-'}</td>
                <td style="font-weight:700;">${r.SUBTOTAL > 0 ? fmtBRL(r.SUBTOTAL) : '-'}</td>
                <td>${r.VAL_RATEIO > 0 ? fmtBRL(r.VAL_RATEIO) : '-'}</td>
                <td>${r.TAXA > 0 ? fmtBRL(r.TAXA) : '-'}</td>
                <td style="font-weight:800; color:#fbbf24;">${r.TOTAL > 0 ? fmtBRL(r.TOTAL) : '-'}</td>
            </tr>
        `).join('');

        box.innerHTML = `
            <table id="dataTable" class="filterable-table">
                <thead>
                    <tr>
                        <th>Unidade</th><th>Tipo</th><th>Hidrômetro</th><th>Medidor</th><th>Data Leit.</th>
                        <th>L. Ant.</th><th>L. Atual</th><th>Consumo (m³)</th><th>Fixa Água</th><th>Fixa Esg.</th>
                        <th>Var. Água</th><th>Var. Esg.</th><th>Subtotal Unid.</th><th>Rateio Fração</th><th>Taxa</th><th>TOTAL UNID.</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
        filtrarTabelaVisivel();
    }

    function renderizarAbaCobranca(box, dadosModo) {
        let rows = dadosModo.cobranca_consolidada.map(r => `
            <tr>
                <td>${r.BLOCO || '-'}</td>
                <td>${r.ANDAR || '-'}</td>
                <td style="font-weight:700;">${r.UNIDADE}</td>
                <td>${badgeTipo(r.TIPO)}</td>
                <td>${r.QTD_MED}</td>
                <td>${(r.FRACAO * 100).toFixed(4)}%</td>
                <td style="font-weight:700; color:#34d399;">${r.TIPO === 'SEM_MEDIDOR' ? '-' : fmtNum(r.CONSUMO) + ' m³'}</td>
                <td>${fmtBRL(r.SUBTOTAL)}</td>
                <td>${r.VAL_RATEIO > 0 ? fmtBRL(r.VAL_RATEIO) : 'R$ 0,00'}</td>
                <td>${fmtBRL(r.TAXA)}</td>
                <td style="font-weight:800; color:#fbbf24; font-size:14px;">${fmtBRL(r.TOTAL)}</td>
            </tr>
        `).join('');

        box.innerHTML = `
            <div class="exec-title green" style="margin-bottom:12px;">
                 LISTAGEM CONSOLIDADA POR UNIDADE PARA BOLETO / ADMINISTRADORA ÂNCORA (755 UNIDADES)
            </div>
            <table id="dataTable" class="filterable-table">
                <thead>
                    <tr>
                        <th>Bloco</th><th>Andar</th><th>Unidade</th><th>Categoria</th><th>Qtd Hidrômetros</th>
                        <th>Fração Ideal</th><th>Consumo Total</th><th>Soma Consumo (R$)</th>
                        <th>Fração Área Comum (R$)</th><th>Taxa Leitura</th><th>TOTAL A PAGAR</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
        filtrarTabelaVisivel();
    }

    function renderizarAbaAlarmes(box, dadosModo) {
        const C = dadosModo.resumo.contagem_alarmes;
        let rows = dadosModo.alarmes_lista.map(a => `
            <tr>
                <td style="font-weight:700;">${a.UNIDADE}</td>
                <td>${badgeTipo(a.TIPO)}</td>
                <td>${a.HIDROMETRO}</td>
                <td>${a.MEDIDOR}</td>
                <td>${a.DATA_LEITURA}</td>
                <td>${fmtNum(a.L_ANTERIOR)}</td>
                <td>${fmtNum(a.VAL_ATU)}</td>
                <td style="font-weight:700;">${fmtNum(a.CONSUMO)} m³</td>
                <td style="color:#f87171; font-weight:700; text-align:left;">⚠️ ${a.ALERTAS}</td>
            </tr>
        `).join('');

        box.innerHTML = `
            <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(170px,1fr)); gap:10px; margin-bottom:16px;">
                <div class="kpi-card" style="padding:12px;"><div><h4>Medidor Bloqueado</h4><h2 style="color:#f87171;">${C.bloqueado}</h2></div></div>
                <div class="kpi-card" style="padding:12px;"><div><h4>Vazamento</h4><h2 style="color:#fbbf24;">${C.vazamento}</h2></div></div>
                <div class="kpi-card" style="padding:12px;"><div><h4>Sub-vazão</h4><h2 style="color:#38bdf8;">${C.subvazao}</h2></div></div>
                <div class="kpi-card" style="padding:12px;"><div><h4>Fluxo Reverso</h4><h2 style="color:#a78bfa;">${C.retorno}</h2></div></div>
                <div class="kpi-card" style="padding:12px;"><div><h4>Desmontagem/Corte</h4><h2 style="color:#f472b6;">${C.desmontagem}</h2></div></div>
                <div class="kpi-card" style="padding:12px;"><div><h4>Sem Transmissão</h4><h2 style="color:#fb923c;">${C.sem_transmissao}</h2></div></div>
            </div>
            <table id="dataTable" class="filterable-table">
                <thead>
                    <tr>
                        <th>Unidade</th><th>Tipo</th><th>Hidrômetro</th><th>Medidor</th>
                        <th>Data Leitura</th><th>L. Ant.</th><th>L. Atual</th><th>Consumo</th><th>Diagnóstico de Alarme</th>
                    </tr>
                </thead>
                <tbody>${rows || '<tr><td colspan="9">Nenhum alarme registrado neste lote.</td></tr>'}</tbody>
            </table>
        `;
        filtrarTabelaVisivel();
    }

    function renderizarComparativo(box) {
        if (!estado.MIZAR || !estado.WALKBY) {
            box.innerHTML = `<div style="padding:45px; text-align:center; color:#94a3b8;">
                <h3>⚖️ Comparativo Mizar vs. Walk-by</h3>
                <p>Para visualizar o confronto lado a lado, carregue os dois arquivos: <b>2. Processar MIZAR</b> e <b>3. Processar WALK-BY</b>.</p>
            </div>`;
            return;
        }

        const RM = estado.MIZAR.resumo;
        const RW = estado.WALKBY.resumo;

        document.getElementById('kpi1-title').innerText = "DIFERENÇA CONSUMO (WALKBY - MIZAR)";
        document.getElementById('kpi2-title').innerText = "ARRECADAÇÃO BASE (WALKBY vs MIZAR)";
        document.getElementById('kpi3-title').innerText = "M³ COMERCIAL (WALKBY vs MIZAR)";
        document.getElementById('kpi4-title').innerText = "REDUÇÃO NO RATEIO (ECONOMIA MORADOR)";

        document.getElementById('lbl-consumo').innerText = "+" + fmtNum(RW.m3_unidades_res - RM.m3_unidades_res) + " m³";
        document.getElementById('lbl-arr-res').innerText = "+" + fmtBRL(RW.consumo_rs_unidades - RM.consumo_rs_unidades);
        document.getElementById('lbl-arr-com').innerText = `${fmtBRL(RW.preco_m3_com)} vs ${fmtBRL(RM.preco_m3_com)}`;
        document.getElementById('lbl-deficit').innerText = fmtBRL(RW.valor_a_ratear - RM.valor_a_ratear);

        const mapMizar = {};
        estado.MIZAR.cobranca_consolidada.forEach(u => { mapMizar[u.UNIDADE] = u; });

        let rowsComp = estado.WALKBY.cobranca_consolidada.map(uw => {
            const um = mapMizar[uw.UNIDADE] || { CONSUMO: 0, SUBTOTAL: 0, VAL_RATEIO: 0, TAXA: 0, TOTAL: 0 };
            const diffM3 = uw.CONSUMO - um.CONSUMO;
            const diffTotal = uw.TOTAL - um.TOTAL;
            return `
                <tr>
                    <td style="font-weight:700;">${uw.UNIDADE}</td>
                    <td>${badgeTipo(uw.TIPO)}</td>
                    <td style="color:#60a5fa;">${fmtNum(um.CONSUMO)} m³</td>
                    <td>${fmtBRL(um.SUBTOTAL)}</td>
                    <td>${fmtBRL(um.VAL_RATEIO)}</td>
                    <td style="font-weight:700; color:#60a5fa;">${fmtBRL(um.TOTAL)}</td>
                    <td style="color:#34d399;">${fmtNum(uw.CONSUMO)} m³</td>
                    <td>${fmtBRL(uw.SUBTOTAL)}</td>
                    <td>${fmtBRL(uw.VAL_RATEIO)}</td>
                    <td style="font-weight:700; color:#34d399;">${fmtBRL(uw.TOTAL)}</td>
                    <td style="font-weight:700; color:${diffM3 !== 0 ? '#fbbf24' : '#94a3b8'};">${diffM3 > 0 ? '+' : ''}${fmtNum(diffM3)} m³</td>
                    <td style="font-weight:800; color:${diffTotal > 0 ? '#f87171' : '#34d399'};">${diffTotal > 0 ? '+' : ''}${fmtBRL(diffTotal)}</td>
                </tr>
            `;
        }).join('');

        box.innerHTML = `
            <div class="exec-section">
                <div class="exec-title">CONFRONTO EXECUTIVO CONSOLIDADO — MIZAR vs. WALK-BY</div>
                <table style="margin-bottom:18px;">
                    <thead>
                        <tr>
                            <th>Modalidade</th>
                            <th>Consumo Unidades (m³)</th>
                            <th>Arrecadação Consumo (R$)</th>
                            <th>Taxa Leitura Unidades</th>
                            <th>Consumo Área Comum (R$)</th>
                            <th>Valor Rateado (Fração)</th>
                            <th>Valor m³ Comercial</th>
                            <th>Fechamento Total</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr style="color:#60a5fa; font-weight:700;">
                            <td>💧 MIZAR (Telemetria)</td>
                            <td>${fmtNum(RM.m3_unidades_res)} m³</td>
                            <td>${fmtBRL(RM.consumo_rs_unidades)}</td>
                            <td>${fmtBRL(RM.taxa_leitura_unidades)}</td>
                            <td>${fmtBRL(RM.consumo_rs_comum)}</td>
                            <td>${fmtBRL(RM.valor_a_ratear)}</td>
                            <td>${fmtBRL(RM.preco_m3_com)} / m³ (${fmtNum(RM.m3_comercial_5unid)} m³)</td>
                            <td>${fmtBRL(RM.despesa_total_condominio)}</td>
                        </tr>
                        <tr style="color:#34d399; font-weight:700;">
                            <td>🚶 WALK-BY (Antena)</td>
                            <td>${fmtNum(RW.m3_unidades_res)} m³</td>
                            <td>${fmtBRL(RW.consumo_rs_unidades)}</td>
                            <td>${fmtBRL(RW.taxa_leitura_unidades)}</td>
                            <td>${fmtBRL(RW.consumo_rs_comum)}</td>
                            <td>${fmtBRL(RW.valor_a_ratear)}</td>
                            <td>${fmtBRL(RW.preco_m3_com)} / m³ (${fmtNum(RW.m3_comercial_5unid)} m³)</td>
                            <td>${fmtBRL(RW.despesa_total_condominio)}</td>
                        </tr>
                    </tbody>
                </table>

                <div class="exec-title green">COMPARATIVO UNIDADE POR UNIDADE (755 UNIDADES)</div>
                <table id="dataTable" class="filterable-table">
                    <thead>
                        <tr>
                            <th>Unidade</th><th>Tipo</th>
                            <th>Mizar (m³)</th><th>Mizar Cons. (R$)</th><th>Mizar Rateio</th><th>MIZAR TOTAL</th>
                            <th>Walk-by (m³)</th><th>Walk-by Cons. (R$)</th><th>Walk-by Rateio</th><th>WALK-BY TOTAL</th>
                            <th>Dif. Volume</th><th>Dif. Financeira</th>
                        </tr>
                    </thead>
                    <tbody>${rowsComp}</tbody>
                </table>
            </div>
        `;
        filtrarTabelaVisivel();
    }

    function filtrarTabelaVisivel() {
        let termo = document.getElementById("searchInput").value.toUpperCase();
        let tables = document.querySelectorAll(".filterable-table");
        tables.forEach(t => {
            let trs = t.getElementsByTagName("tr");
            for (let i = 1; i < trs.length; i++) {
                let txt = trs[i].innerText.toUpperCase();
                trs[i].style.display = txt.includes(termo) ? "" : "none";
            }
        });
    }

    function exportarExcelCompleto() {
        const wb = XLSX.utils.book_new();
        let adicionou = false;

        ['WALKBY', 'MIZAR'].forEach(modo => {
            if (!estado[modo]) return;
            adicionou = true;
            const D = estado[modo];
            const pref = modo.toLowerCase();

            // 1. Aba Geral
            const wsGeral = XLSX.utils.json_to_sheet([D.resumo]);
            XLSX.utils.book_append_sheet(wb, wsGeral, `${pref} geral`);

            // 2. Aba Unidades (1576 linhas)
            const wsUnds = XLSX.utils.json_to_sheet(D.unidades_hidrometros);
            XLSX.utils.book_append_sheet(wb, wsUnds, `${pref} unds`);

            // 3. Aba Cobrança Consolidada (755 unidades)
            const wsCob = XLSX.utils.json_to_sheet(D.cobranca_consolidada);
            XLSX.utils.book_append_sheet(wb, wsCob, `${pref} cobrança`);

            // 4. Aba Alarmes
            const wsAlm = XLSX.utils.json_to_sheet(D.alarmes_lista);
            XLSX.utils.book_append_sheet(wb, wsAlm, `${pref} alarmes`);
        });

        if (!adicionou) return alert("Processe pelo menos um arquivo (Mizar ou Walk-by) antes de exportar!");
        XLSX.writeFile(wb, "COBRANCA_DE_AGUA_BRISAS_DO_LAGO.xlsx");
    }

    function exportarPDFExecutivo() {
        const modo = estado.moduloAtivo === 'COMPARATIVO' ? (estado.WALKBY ? 'WALKBY' : 'MIZAR') : estado.moduloAtivo;
        const D = estado[modo];
        if (!D) return alert("Processe os dados primeiro antes de gerar o PDF!");

        const R = D.resumo;
        const { jsPDF } = window.jspdf;
        const doc = new jsPDF('p', 'mm', 'a4');

        doc.setFillColor(17, 28, 45);
        doc.rect(0, 0, 210, 28, 'F');
        doc.setTextColor(255, 255, 255);
        doc.setFontSize(14);
        doc.setFont("helvetica", "bold");
        doc.text(`CONDOMÍNIO BRISAS DO LAGO — DEMONSTRATIVO GERENCIAL (${modo})`, 105, 12, { align: "center" });
        doc.setFontSize(9);
        doc.setFont("helvetica", "normal");
        doc.text("Apuração Oficial ADASA nº 70/2026 | Residencial, Comercial Rateado e Áreas Comuns", 105, 18, { align: "center" });
        doc.text("Emitido em: " + new Date().toLocaleString('pt-BR'), 105, 24, { align: "center" });

        let y = 35;
        doc.setTextColor(0, 0, 0);
        doc.setFontSize(10);
        doc.setFont("helvetica", "bold");
        doc.text("1. FECHAMENTO FINANCEIRO RESIDENCIAL E RATEIO DE ÁREA COMUM", 14, y);

        doc.autoTable({
            startY: y + 3,
            head: [['Indicador Financeiro', 'Volume / Base', 'Valor Apurado (R$)']],
            body: [
                ['Fatura CAESB Geral (Hidrômetro Externo)', fmtNum(R.m3_caesb_res) + ' m³', fmtBRL(R.fatura_caesb_res)],
                ['Taxa de Leitura Total Condomínio (1.569 medidores)', 'R$ 7,90 / medidor', fmtBRL(R.taxa_leitura_total)],
                ['DESPESA TOTAL A SER PAGA (Fatura CAESB + Leitura)', 'Meta de Cobertura', fmtBRL(R.despesa_total_condominio)],
                ['(-) Arrecadação Consumo das Unidades (Empório + 750 Aptos)', fmtNum(R.m3_unidades_res) + ' m³', fmtBRL(R.consumo_rs_unidades)],
                ['(-) Taxa de Leitura Cobrada das Unidades (1.533 medidores)', 'Unidades Autônomas', fmtBRL(R.taxa_leitura_unidades)],
                ['(=) VALOR TOTAL RATEADO POR FRAÇÃO IDEAL', '100,0000% Fração', fmtBRL(R.valor_a_ratear)]
            ],
            theme: 'grid',
            headStyles: { fillColor: [26, 115, 232] },
            columnStyles: { 2: { halign: 'right', fontStyle: 'bold' } }
        });

        y = doc.lastAutoTable.finalY + 10;
        doc.text("2. DEMONSTRATIVO DO SETOR COMERCIAL (RATEIO DIRETO DA CONTA COMERCIAL)", 14, y);

        const comRows = D.unidades_hidrometros.filter(x => x.TIPO === 'COMERCIAL' || x.TIPO === 'COMERCIAL_OMO');
        doc.autoTable({
            startY: y + 3,
            head: [['Unidade Comercial', 'Medidor', 'L. Ant.', 'L. Atual', 'Consumo', 'Valor Água', 'Tx. Leitura', 'Total (R$)']],
            body: comRows.map(c => [
                c.UNIDADE_RAW, c.MEDIDOR_VISUAL, fmtNum(c.L_ANTERIOR), fmtNum(c.VAL_ATU),
                fmtNum(c.CONSUMO) + ' m³', fmtBRL(c.V_AGUA), fmtBRL(c.TAXA), fmtBRL(c.V_AGUA + c.TAXA)
            ]),
            theme: 'striped',
            headStyles: { fillColor: [217, 119, 6] },
            columnStyles: { 5: { halign: 'right' }, 6: { halign: 'right' }, 7: { halign: 'right', fontStyle: 'bold' } }
        });

        y = doc.lastAutoTable.finalY + 10;
        doc.text("3. AUDITORIA DE ALARMES E ANOMALIAS TÉCNICAS", 14, y);
        const C = R.contagem_alarmes;
        doc.autoTable({
            startY: y + 3,
            head: [['Medidor Bloqueado', 'Vazamento', 'Sub-vazão', 'Fluxo Reverso', 'Desmontagem', 'Sem Transmissão']],
            body: [[C.bloqueado, C.vazamento, C.subvazao, C.retorno, C.desmontagem, C.sem_transmissao]],
            theme: 'grid',
            headStyles: { fillColor: [220, 38, 38], halign: 'center' },
            bodyStyles: { halign: 'center', fontStyle: 'bold' }
        });

        doc.save(`Demonstrativo_Executivo_${modo}_Brisas.pdf`);
    }
</script>
</body>
</html>
"""

# ==============================================================================
# ROTAS FLASK BACKEND
# ==============================================================================
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/estado_inicial', methods=['GET'])
def estado_inicial():
    out = {}
    for ch in ['MIZAR', 'WALKBY']:
        reg = EstadoCalculoV3.query.filter_by(chave=ch).first()
        if reg and reg.dados_json:
            try: out[ch] = json.loads(reg.dados_json)
            except: pass
    return jsonify(out)

@app.route('/upload_base', methods=['POST'])
def upload_base():
    if 'file' not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado."}), 400
    f = request.files['file']
    try:
        info = MotorFaturamento.ler_planilha_ou_txt(f.read(), f.filename)
        tipo_arq = info['tipo_arquivo']
        df = info['df']

        MedidorBaseV3.query.delete()
        adicionados = 0
        tem_251 = False

        if tipo_arq == 'PLANILHA_MESTRA_COBRANCA':
            # Extrai diretamente das linhas 57 a 1633 da aba COBRANÇA oficial
            for idx in range(56, len(df)):
                row = df.iloc[idx]
                bloco = str(row[0]).strip() if pd.notna(row[0]) else ""
                andar = str(row[1]).strip() if pd.notna(row[1]) else ""
                unid_col = str(row[2]).strip() if pd.notna(row[2]) else ""
                hidro = str(row[3]).strip() if pd.notna(row[3]) else ""
                med_vis = str(row[4]).strip().split('.')[0] if pd.notna(row[4]) else ""

                if not unid_col or unid_col.upper() in ['NAN', 'NONE', 'UNIDADE', 'TOTAL']:
                    continue

                dna = MotorFaturamento.limpar_id_medidor(med_vis)
                unid_grupo = MotorFaturamento.normalizar_unidade_grupo(unid_col, bloco, andar)
                ordem_num = adicionados + 1
                tipo_unid = MotorFaturamento.classificar_tipo(unid_grupo, med_vis, ordem_num)

                if tipo_unid == 'SEM_MEDIDOR':
                    tem_251 = True
                    dna = f"SEM_MED_251_{ordem_num}"
                    taxa_val = 0.0
                else:
                    if not dna or dna == '-': continue
                    taxa_val = 7.90

                if MedidorBaseV3.query.filter_by(medidor_dna=dna).first():
                    continue

                dt_ant = str(row[5]).strip().split(' ')[0] if pd.notna(row[5]) else "04/09/2026"
                l_ant = MotorFaturamento.limpar_num(row[6], 0.0)
                frac = MotorFaturamento.limpar_num(row[20], 0.0)

                db.session.add(MedidorBaseV3(
                    ordem=ordem_num, bloco=bloco, andar=andar,
                    unidade=unid_grupo, unidade_raw=unid_col, tipo=tipo_unid,
                    hidrometro=hidro, medidor_visual=med_vis, medidor_dna=dna,
                    fracao=frac, taxa=taxa_val, l_anterior=l_ant, data_anterior=dt_ant
                ))
                adicionados += 1

        else:
            # Tabela simples (brisas.xlsx ou walkby.txt usado como base)
            df.columns = [MotorFaturamento.limpa_nome_coluna(c) for c in df.columns]
            col_uni = next((c for c in df.columns if 'LOCAL' in c or 'UNIDADE' in c), None)
            col_med = next((c for c in df.columns if 'MEDIDOR' in c), None)
            col_hid = next((c for c in df.columns if 'TIPO' in c or 'DESCRICAO' in c or 'HIDRO' in c), None)
            col_ant = next((c for c in df.columns if 'ANTIGO' in c or 'ANTERIOR' in c or 'INDICE' in c), None)

            if not col_uni or not col_med:
                return jsonify({"error": "Colunas de Localização/Unidade e Medidor não encontradas."}), 400

            for idx, r in df.iterrows():
                unid_raw = str(r[col_uni]).strip()
                med_vis = str(r[col_med]).strip().split('.')[0]
                dna = MotorFaturamento.limpar_id_medidor(med_vis)
                if not dna or dna == '-': continue
                if MedidorBaseV3.query.filter_by(medidor_dna=dna).first(): continue

                adicionados += 1
                unid_grupo = MotorFaturamento.normalizar_unidade_grupo(unid_raw)
                tipo_unid = MotorFaturamento.classificar_tipo(unid_grupo, med_vis, adicionados)
                l_ant = MotorFaturamento.limpar_num(r[col_ant], 0.0) if col_ant else 0.0
                hidro = str(r[col_hid]).strip() if col_hid and pd.notna(r[col_hid]) else ""

                # Fração padrão aproximada caso o arquivo simples não contenha a coluna U de fração
                frac_default = 0.002411 if tipo_unid == 'EMPORIO' else (0.994838 / 749.0 if tipo_unid == 'RESIDENCIAL' else 0.0)

                db.session.add(MedidorBaseV3(
                    ordem=adicionados, bloco="", andar="",
                    unidade=unid_grupo, unidade_raw=unid_raw, tipo=tipo_unid,
                    hidrometro=hidro, medidor_visual=med_vis, medidor_dna=dna,
                    fracao=frac_default, taxa=7.90, l_anterior=l_ant
                ))

        # Garante que a Unidade B - 2º - 251 (Sem Medidor) esteja sempre presente!
        if not tem_251:
            db.session.add(MedidorBaseV3(
                ordem=569, bloco="B", andar="2º",
                unidade="B - 2º - 251", unidade_raw="B - 251", tipo="SEM_MEDIDOR",
                hidrometro="-", medidor_visual="-", medidor_dna="SEM_MED_251_1",
                fracao=0.002751, taxa=0.0, l_anterior=0.0, data_anterior="-"
            ))
            adicionados += 1

        db.session.commit()
        return jsonify({"message": f"Base Oficial carregada com sucesso! {adicionados} registros configurados (incluindo regra especial da Unidade 251 e Empório)."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/processar', methods=['POST'])
def processar():
    if 'file' not in request.files:
        return jsonify({"error": "Arquivo de leitura não enviado."}), 400
    f = request.files['file']
    modo = request.form.get('modo', 'WALKBY').upper()
    val_res = MotorFaturamento.limpar_num(request.form.get('caesb_res', 62562.80), 62562.80)
    val_com = MotorFaturamento.limpar_num(request.form.get('caesb_com', 6452.54), 6452.54)
    m3_res = MotorFaturamento.limpar_num(request.form.get('m3_res', 5248), 5248)
    m3_com = MotorFaturamento.limpar_num(request.form.get('m3_com', 162), 162)

    try:
        info = MotorFaturamento.ler_planilha_ou_txt(f.read(), f.filename)
        df_mes = info['df']

        resultado = MotorFaturamento.executar_fechamento(
            df_mes=df_mes, modo_leitura=modo,
            val_caesb_res=val_res, val_caesb_com=val_com,
            m3_caesb_res=m3_res, m3_caesb_com=m3_com
        )

        # Salva estado no banco para persistência entre reloads
        reg = EstadoCalculoV3.query.filter_by(chave=modo).first()
        if not reg:
            reg = EstadoCalculoV3(chave=modo, dados_json=json.dumps(resultado))
            db.session.add(reg)
        else:
            reg.dados_json = json.dumps(resultado)
        db.session.commit()

        return jsonify(resultado)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, port=port, host='0.0.0.0')
