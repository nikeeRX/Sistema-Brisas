import os
import io
import re
import csv
import json
import unicodedata
import pandas as pd
from flask import Flask, render_template_string, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

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

# --- MODELOS DO BANCO DE DADOS (BASE FIXA PERMANENTE) ---
class MedidorBaseV4(db.Model):
    __tablename__ = 'medidor_base_v4'
    id = db.Column(db.Integer, primary_key=True)
    ordem = db.Column(db.Integer, default=0)
    bloco = db.Column(db.String(20), default="")
    andar = db.Column(db.String(20), default="")
    unidade = db.Column(db.String(100))          # Ex: A - S1 - 001, KITCLEAN
    unidade_curta = db.Column(db.String(50))     # Ex: 001, 101
    unidade_raw = db.Column(db.String(100))      # Ex: KITCLEAN 1
    tipo = db.Column(db.String(50))              # RESIDENCIAL, EMPORIO, COMERCIAL, COMERCIAL_OMO, AREA_COMUM, SEM_MEDIDOR
    hidrometro = db.Column(db.String(100), default="-")
    medidor_visual = db.Column(db.String(100))
    medidor_dna = db.Column(db.String(100), unique=True)
    fracao = db.Column(db.Float, default=0.0)
    taxa = db.Column(db.Float, default=7.90)
    l_anterior = db.Column(db.Float, default=0.0)
    data_anterior = db.Column(db.String(50), default="04/09/2026")

class EstadoCalculoV4(db.Model):
    __tablename__ = 'estado_calculo_v4'
    id = db.Column(db.Integer, primary_key=True)
    chave = db.Column(db.String(50), unique=True) # 'MIZAR' ou 'WALKBY'
    dados_json = db.Column(db.Text)


# --- MOTOR MATEMÁTICO E REGRAS DE NEGÓCIO BRISAS DO LAGO ---
class MotorFaturamento:
    AREAS_COMUNS_KEYS = [
        'ACADEMIA', 'JACUZZI', 'BANHEIRO', 'CONDOMINIO', 'LAVA PE', 'LIXEIRA',
        'ROUPARIA', 'SALAO GOURMET', 'SAUNA', 'SPAS', 'TANQUES LAVANDERIA EXCELE',
        'TORNEIRA', 'ESPACO PET', 'GARAGEM'
    ]

    @staticmethod
    def remover_acentos(txt):
        if txt is None or pd.isna(txt): return ""
        u = str(txt).upper().strip()
        return ''.join(c for c in unicodedata.normalize('NFKD', u) if not unicodedata.combining(c))

    @staticmethod
    def extrair_bloco_andar_unid(texto_local, bloco_in="", andar_in=""):
        u_clean = MotorFaturamento.remover_acentos(texto_local)
        b = str(bloco_in).strip() if bloco_in and str(bloco_in).strip().upper() not in ['', '-', 'NAN', 'NONE'] else ""
        a = str(andar_in).strip() if andar_in and str(andar_in).strip().upper() not in ['', '-', 'NAN', 'NONE'] else ""

        if 'KITCLEAN' in u_clean or 'KIT CLEAN' in u_clean:
            return ("-", "-", "KITCLEAN", "KITCLEAN")
        if 'BLUMA' in u_clean or 'SALAO BELEZA' in u_clean:
            return ("-", "-", "SALÃO BELEZA BLUMA", "SALÃO BELEZA BLUMA")
        if 'LAVANDERIA OMO' in u_clean:
            return ("-", "-", "LAVANDERIA OMO", "LAVANDERIA OMO")
        if 'EMPORIO' in u_clean:
            return ("-", "-", "EMPÓRIO", "EMPÓRIO")
        if 'RESTAURANTE' in u_clean:
            return ("-", "-", "RESTAURANTE", "RESTAURANTE")
        if 'CYBER' in u_clean:
            return ("-", "-", "CYBER CAFÉ", "CYBER CAFÉ")

        if b and a and re.match(r'^\d+$', u_clean):
            u_num = u_clean.zfill(3)
            return (b, a, f"{b} - {a} - {u_num}", u_num)

        partes = [p.strip() for p in u_clean.split('-') if p.strip()]
        if len(partes) == 3 and partes[0] in ['A', 'B', 'C', 'D', 'E']:
            bloco = partes[0]
            andar = partes[1]
            if andar in ['1', '2', '3', '1O', '2O', '3O']:
                andar = f"{andar[0]}º"
            unid_num = partes[2].zfill(3) if partes[2].isdigit() else partes[2]
            return (bloco, andar, f"{bloco} - {andar} - {unid_num}", unid_num)

        if len(partes) == 2 and partes[0] in ['A', 'B', 'C', 'D', 'E'] and partes[1].isdigit():
            bloco = partes[0]
            unid_num = partes[1].zfill(3)
            andar = f"{unid_num[0]}º" if unid_num[0] in ['1', '2', '3'] else "T"
            return (bloco, andar, f"{bloco} - {andar} - {unid_num}", unid_num)

        return (b or "-", a or "-", str(texto_local).strip(), str(texto_local).strip())

    @staticmethod
    def classificar_tipo(unidade_raw, medidor_str="", ordem=0):
        u = MotorFaturamento.remover_acentos(unidade_raw)
        med = str(medidor_str).strip()
        if med == '-' or ('251' in u and ('B - 2' in u or 'B-2' in u or u == '251')):
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
        if s.upper() in ['', '-', 'NAN', 'NONE', 'NULL']: return default
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
    def inicializar_base_padrao_se_vazia():
        """Garante que a Base Fixa NUNCA fique vazia ao abrir o sistema"""
        if MedidorBaseV4.query.count() > 0:
            return

        # 1. Tenta resgatar automaticamente dados de tabelas anteriores no mesmo banco Railway
        try:
            res = db.session.execute(text("SELECT unidade, medidor_visual, medidor_dna, fracao, taxa, l_anterior FROM medidor_base_v3")).fetchall()
            if res and len(res) > 10:
                objs = []
                for idx, r in enumerate(res):
                    b, a, u_grp, u_curta = MotorFaturamento.extrair_bloco_andar_unid(r[0])
                    t_unid = MotorFaturamento.classificar_tipo(u_grp, r[1], idx + 1)
                    objs.append(MedidorBaseV4(
                        ordem=idx+1, bloco=b, andar=a, unidade=u_grp, unidade_curta=u_curta,
                        unidade_raw=r[0], tipo=t_unid, hidrometro="-", medidor_visual=r[1],
                        medidor_dna=r[2], fracao=float(r[3] or 0.0), taxa=float(r[4] or 7.90),
                        l_anterior=float(r[5] or 0.0)
                    ))
                db.session.bulk_save_objects(objs)
                db.session.commit()
                return
        except Exception:
            db.session.rollback()

        # 2. Semeia a estrutura mestre inicial do Brisas do Lago para exibição imediata das colunas
        seed_inicial = [
            (1, "-", "-", "EMPÓRIO", "EMPÓRIO", "EMPORIO", "EMPORIO", "A23B182990", "23419692", "23419692", 0.002411, 7.90, 130),
            (2, "-", "-", "RESTAURANTE", "RESTAURANTE", "RESTAURANTE", "COMERCIAL", "A23B183018", "23712149", "23712149", 0.0, 7.90, 1330),
            (3, "-", "-", "CYBER CAFÉ", "CYBER CAFÉ", "CYBER CAFÉ", "COMERCIAL", "A23B590261", "32207964", "32207964", 0.0, 7.90, 150),
            (4, "-", "-", "KITCLEAN", "KITCLEAN", "KITCLEAN 1", "COMERCIAL", "A22B723087", "22600225", "22600225", 0.0, 7.90, 693),
            (5, "-", "-", "KITCLEAN", "KITCLEAN", "KITCLEAN 2", "COMERCIAL", "A23B590265", "32207969", "32207969", 0.0, 7.90, 564),
            (6, "-", "-", "SALÃO BELEZA BLUMA", "SALÃO BELEZA BLUMA", "SALAO BELEZA BLUMA 1", "COMERCIAL", "A23B183116", "23711360", "23711360", 0.0, 7.90, 31),
            (7, "-", "-", "SALÃO BELEZA BLUMA", "SALÃO BELEZA BLUMA", "SALAO BELEZA BLUMA 2", "COMERCIAL", "B23DM0401067", "32500619", "32500619", 0.0, 7.90, 19),
            (8, "A", "S1", "A - S1 - 001", "001", "A - S1 - 001", "RESIDENCIAL", "A22DM2511768", "21701537", "21701537", 0.001492, 7.90, 119),
            (9, "A", "S1", "A - S1 - 001", "001", "A - S1 - 001", "RESIDENCIAL", "A22DM2511770", "21702167", "21702167", 0.0, 7.90, 120),
            (10, "A", "S1", "A - S1 - 002", "002", "A - S1 - 002", "RESIDENCIAL", "A23B182902", "23418855", "23418855", 0.001634, 7.90, 248),
            (11, "A", "S1", "A - S1 - 002", "002", "A - S1 - 002", "RESIDENCIAL", "B23DM0299449", "23005042", "23005042", 0.0, 7.90, 91),
            (569, "B", "2º", "B - 2º - 251", "251", "B - 251", "SEM_MEDIDOR", "-", "-", "SEM_MED_251_1", 0.002751, 0.0, 0),
            (1540, "-", "-", "LAVANDERIA OMO", "LAVANDERIA OMO", "LAVANDERIA OMO", "COMERCIAL_OMO", "A23B183056", "23713002", "23713002", 0.0, 7.90, 3519),
            (1541, "-", "-", "ACADEMIA WCs CORREDOR", "ACADEMIA WCs CORREDOR", "ACADEMIA WCs CORREDOR", "AREA_COMUM", "A22B761833", "23006074", "23006074", 0.0, 7.90, 3044),
        ]
        objs = [
            MedidorBaseV4(
                ordem=s[0], bloco=s[1], andar=s[2], unidade=s[3], unidade_curta=s[4],
                unidade_raw=s[5], tipo=s[6], hidrometro=s[7], medidor_visual=s[8],
                medidor_dna=s[9], fracao=s[10], taxa=s[11], l_anterior=s[12]
            ) for s in seed_inicial
        ]
        db.session.bulk_save_objects(objs)
        db.session.commit()

    @staticmethod
    def obter_preview_base_fixa():
        """Retorna as linhas da Base Fixa para exibir a tabela imediatamente ao abrir o site"""
        MotorFaturamento.inicializar_base_padrao_se_vazia()
        medidores = MedidorBaseV4.query.order_by(MedidorBaseV4.ordem, MedidorBaseV4.id).all()
        rows = []
        vistos_unid = set()
        for m in medidores:
            prim = m.unidade not in vistos_unid
            vistos_unid.add(m.unidade)
            fixa = 11.62 if (prim and m.tipo in ['RESIDENCIAL', 'SEM_MEDIDOR']) else 0.0
            rows.append({
                'ORDEM': m.ordem,
                'BLOCO': m.bloco or '-',
                'ANDAR': m.andar or '-',
                'UNIDADE': m.unidade,
                'UNIDADE_CURTA': m.unidade_curta or m.unidade,
                'UNIDADE_RAW': m.unidade_raw,
                'TIPO': m.tipo,
                'HIDROMETRO': m.hidrometro or '-',
                'MEDIDOR_VISUAL': m.medidor_visual or '-',
                'DATA_LEITURA': m.data_anterior or 'Base Fixa',
                'L_ANTERIOR': m.l_anterior,
                'VAL_ATU': m.l_anterior,
                'CONSUMO': 0,
                'FIXA_AGUA': fixa,
                'FIXA_ESGOTO': fixa,
                'V_AGUA': 0.0,
                'V_ESGOTO': 0.0,
                'SUBTOTAL': fixa * 2 if prim else 0.0,
                'VAL_RATEIO': 0.0,
                'TAXA': m.taxa,
                'TOTAL': (fixa * 2) + m.taxa if prim else m.taxa
            })
        return rows

    @staticmethod
    def extrair_dados_conta_caesb_texto(texto_bruto):
        t = str(texto_bruto)
        t_clean = re.sub(r'\s+', ' ', t)

        out = {
            'data_ant': '', 'leitura_ant': 0,
            'data_atu': '', 'leitura_atu': 0,
            'volume_m3': 0,
            'fixa_agua': 0.0, 'fixa_esgoto': 0.0,
            'var_agua': 0.0, 'var_esgoto': 0.0,
            'total_fatura': 0.0,
            'categoria': 'RESIDENCIAL'
        }

        if 'COMERCIAL' in t_clean.upper():
            out['categoria'] = 'COMERCIAL'

        pares_data_leit = re.findall(r'(\d{2}/\d{2}/\d{4})\s+(\d{3,7})', t_clean)
        pares_validos = [p for p in pares_data_leit if not p[0].endswith(('2019', '2020', '2021', '2022'))]
        if len(pares_validos) >= 2:
            out['data_ant'] = pares_validos[0][0]
            out['leitura_ant'] = int(pares_validos[0][1])
            out['data_atu'] = pares_validos[1][0]
            out['leitura_atu'] = int(pares_validos[1][1])
            if out['leitura_atu'] >= out['leitura_ant']:
                out['volume_m3'] = out['leitura_atu'] - out['leitura_ant']

        if out['volume_m3'] == 0:
            m_vol = re.search(r'(?:VOLUME MEDIDO|CONSUMO FATURADO).*?(\d{2,6})', t_clean, re.IGNORECASE)
            if m_vol:
                out['volume_m3'] = int(m_vol.group(1))

        m_fa = re.search(r'TARIFA\s+FIXA\s+DE\s+AGUA[^\d]*([\d\.]*,\d{2})', t_clean, re.IGNORECASE)
        if m_fa: out['fixa_agua'] = MotorFaturamento.limpar_num(m_fa.group(1))

        m_fe = re.search(r'TARIFA\s+FIXA\s+DE\s+ESGOTO[^\d]*([\d\.]*,\d{2})', t_clean, re.IGNORECASE)
        if m_fe: out['fixa_esgoto'] = MotorFaturamento.limpar_num(m_fe.group(1))

        m_va = re.search(r'TARIFA\s+VARIAVEL\s+DE\s+AGUA[^\d]*([\d\.]*,\d{2})', t_clean, re.IGNORECASE)
        if m_va: out['var_agua'] = MotorFaturamento.limpar_num(m_va.group(1))

        m_ve = re.search(r'TARIFA\s+VARIAVEL\s+DE\s+ESGOTO[^\d]*([\d\.]*,\d{2})', t_clean, re.IGNORECASE)
        if m_ve: out['var_esgoto'] = MotorFaturamento.limpar_num(m_ve.group(1))

        if out['fixa_agua'] > 0 and out['fixa_esgoto'] == 0:
            out['fixa_esgoto'] = out['fixa_agua']
        if out['var_agua'] > 0 and out['var_esgoto'] == 0:
            out['var_esgoto'] = out['var_agua']

        soma_tarifas = round(out['fixa_agua'] + out['fixa_esgoto'] + out['var_agua'] + out['var_esgoto'], 2)

        m_tot = re.search(r'TOTAL\s+A\s+PAGAR.*?([\d\.]+\.\d{3},\d{2}|[\d\.]+,\d{2})', t_clean, re.IGNORECASE)
        if m_tot:
            out['total_fatura'] = MotorFaturamento.limpar_num(m_tot.group(1))

        if soma_tarifas > 0 and (out['total_fatura'] == 0 or abs(out['total_fatura'] - soma_tarifas) > 10):
            out['total_fatura'] = soma_tarifas

        return out

    @staticmethod
    def ler_planilha_ou_txt(file_bytes, filename):
        fname = filename.lower()
        if fname.endswith(('.xlsx', '.xls')):
            xls = pd.ExcelFile(io.BytesIO(file_bytes))
            # 1. Planilha de 6 abas (aba 'COBRANÇA')
            aba_cob = next((s for s in xls.sheet_names if 'COBRAN' in MotorFaturamento.remover_acentos(s) and 'COMERCIAL' not in MotorFaturamento.remover_acentos(s) and 'MODELO' not in MotorFaturamento.remover_acentos(s) and 'WALK' not in MotorFaturamento.remover_acentos(s) and 'MIZAR' not in MotorFaturamento.remover_acentos(s)), None)
            if aba_cob:
                df_raw = pd.read_excel(xls, sheet_name=aba_cob, header=None)
                if len(df_raw) > 100 and df_raw.shape[1] >= 19:
                    return {'tipo_arquivo': 'PLANILHA_6ABAS_COBRANCA', 'df': df_raw}

            # 2. Planilha de 9 abas ('walk-by unds' ou 'mizar unds')
            aba_unds = next((s for s in xls.sheet_names if 'WALK' in MotorFaturamento.remover_acentos(s) and 'UNDS' in MotorFaturamento.remover_acentos(s)), None)
            if not aba_unds:
                aba_unds = next((s for s in xls.sheet_names if 'UNDS' in MotorFaturamento.remover_acentos(s)), None)
            if aba_unds:
                df_u = pd.read_excel(xls, sheet_name=aba_unds, header=None)
                return {'tipo_arquivo': 'PLANILHA_9ABAS_UNDS', 'df': df_u}

            # 3. Aba LEITURA ou planilha simples
            aba_leit = next((s for s in xls.sheet_names if 'LEITURA' in MotorFaturamento.remover_acentos(s)), None)
            if aba_leit:
                df_l = pd.read_excel(xls, sheet_name=aba_leit)
                return {'tipo_arquivo': 'TABELA_SIMPLES', 'df': df_l}

            df = pd.read_excel(xls, dtype=str)
            return {'tipo_arquivo': 'TABELA_SIMPLES', 'df': df}

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
    def importar_ou_atualizar_base_fixa(info):
        """Grava todos os 1.576 medidores em memória e faz 1 único Bulk Insert no banco (< 0.2s)"""
        tipo_arq = info['tipo_arquivo']
        df = info['df']

        # Guarda frações ideais já existentes no banco para não perder se o usuário subir um TXT/CSV simples
        fracoes_existentes = {}
        try:
            for m_old in MedidorBaseV4.query.all():
                if m_old.fracao and m_old.fracao > 0:
                    fracoes_existentes[m_old.unidade] = m_old.fracao
                    fracoes_existentes[m_old.medidor_dna] = m_old.fracao
        except Exception:
            db.session.rollback()

        MedidorBaseV4.query.delete()
        db.session.commit()

        novos_objetos = []
        dnas_vistos = set()
        tem_251 = False

        if tipo_arq in ['PLANILHA_6ABAS_COBRANCA', 'PLANILHA_9ABAS_UNDS']:
            start_row = 56 if tipo_arq == 'PLANILHA_6ABAS_COBRANCA' else 6
            for idx in range(start_row, len(df)):
                row = df.iloc[idx]
                bloco = str(row[0]).strip() if pd.notna(row[0]) and str(row[0]).strip().upper() not in ['NAN', 'NONE'] else ""
                andar = str(row[1]).strip() if pd.notna(row[1]) and str(row[1]).strip().upper() not in ['NAN', 'NONE'] else ""
                unid_col = str(row[2]).strip() if pd.notna(row[2]) and str(row[2]).strip().upper() not in ['NAN', 'NONE'] else ""
                hidro = str(row[3]).strip() if pd.notna(row[3]) and str(row[3]).strip().upper() not in ['NAN', 'NONE'] else "-"
                med_vis = str(row[4]).strip().split('.')[0] if pd.notna(row[4]) and str(row[4]).strip().upper() not in ['NAN', 'NONE'] else "-"

                if not unid_col or unid_col.upper() in ['NAN', 'NONE', 'UNIDADE', 'TOTAL']:
                    continue

                dna = MotorFaturamento.limpar_id_medidor(med_vis)
                b_ext, a_ext, unid_grupo, unid_curta = MotorFaturamento.extrair_bloco_andar_unid(unid_col, bloco, andar)
                ordem_num = len(novos_objetos) + 1
                tipo_unid = MotorFaturamento.classificar_tipo(unid_grupo, med_vis, ordem_num)

                if tipo_unid == 'SEM_MEDIDOR':
                    tem_251 = True
                    dna = f"SEM_MED_251_{ordem_num}"
                    taxa_val = 0.0
                else:
                    if not dna or dna == '-': continue
                    taxa_val = 7.90

                if dna in dnas_vistos:
                    continue
                dnas_vistos.add(dna)

                dt_ant = str(row[5]).strip().split(' ')[0] if pd.notna(row[5]) else "04/09/2026"
                l_ant = MotorFaturamento.limpar_num(row[6], 0.0)

                if tipo_arq == 'PLANILHA_6ABAS_COBRANCA' and len(row) > 20:
                    frac = MotorFaturamento.limpar_num(row[20], 0.0)
                else:
                    rat_rs = MotorFaturamento.limpar_num(row[15], 0.0) if len(row) > 15 else 0.0
                    frac = (rat_rs / 9461.26) if rat_rs > 0 else fracoes_existentes.get(unid_grupo, 0.0)

                novos_objetos.append(MedidorBaseV4(
                    ordem=ordem_num, bloco=b_ext, andar=a_ext,
                    unidade=unid_grupo, unidade_curta=unid_curta, unidade_raw=unid_col, tipo=tipo_unid,
                    hidrometro=hidro, medidor_visual=med_vis, medidor_dna=dna,
                    fracao=frac, taxa=taxa_val, l_anterior=l_ant, data_anterior=dt_ant
                ))
        else:
            df_copy = df.copy()
            # Evita colunas duplicadas que quebram o Pandas
            cols_limpas = []
            for i, c in enumerate(df_copy.columns):
                cl = MotorFaturamento.limpa_nome_coluna(c)
                if not cl or cl in cols_limpas:
                    cl = f"{cl}_{i}"
                cols_limpas.append(cl)
            df_copy.columns = cols_limpas

            col_uni = next((c for c in df_copy.columns if 'LOCAL' in c or 'UNIDADE' in c), None)
            col_med = next((c for c in df_copy.columns if 'MEDIDOR' in c), None)
            col_hid = next((c for c in df_copy.columns if c in ['TIPO', 'DESCRICAO', 'HIDROMETRO'] or 'HIDRO' in c or 'DESCRIC' in c), None)
            col_ant = next((c for c in df_copy.columns if 'ANTIGO' in c or 'ANTERIOR' in c), None)
            if not col_ant:
                col_ant = next((c for c in df_copy.columns if 'INDICE' in c or 'LEITURA' in c), None)

            if not col_uni or not col_med:
                raise ValueError(f"Colunas de Unidade/Medidor não encontradas. Colunas lidas: {list(df.columns)[:6]}")

            for idx, r in df_copy.iterrows():
                unid_raw = str(r[col_uni]).strip()
                med_vis = str(r[col_med]).strip().split('.')[0]
                dna = MotorFaturamento.limpar_id_medidor(med_vis)
                if not dna or dna == '-': continue
                if dna in dnas_vistos: continue
                dnas_vistos.add(dna)

                ordem_num = len(novos_objetos) + 1
                b_ext, a_ext, unid_grupo, unid_curta = MotorFaturamento.extrair_bloco_andar_unid(unid_raw)
                tipo_unid = MotorFaturamento.classificar_tipo(unid_grupo, med_vis, ordem_num)
                l_ant = MotorFaturamento.limpar_num(r[col_ant], 0.0) if col_ant else 0.0
                hidro = str(r[col_hid]).strip() if col_hid and pd.notna(r[col_hid]) and str(r[col_hid]).strip().upper() not in ['NAN', 'NONE', ''] else "-"

                frac_salva = fracoes_existentes.get(dna) or fracoes_existentes.get(unid_grupo)
                if frac_salva and frac_salva > 0:
                    frac_final = frac_salva
                else:
                    frac_final = 0.002411 if tipo_unid == 'EMPORIO' else (0.994838 / 749.0 if tipo_unid == 'RESIDENCIAL' else 0.0)

                novos_objetos.append(MedidorBaseV4(
                    ordem=ordem_num, bloco=b_ext, andar=a_ext,
                    unidade=unid_grupo, unidade_curta=unid_curta, unidade_raw=unid_raw, tipo=tipo_unid,
                    hidrometro=hidro, medidor_visual=med_vis, medidor_dna=dna,
                    fracao=frac_final, taxa=7.90, l_anterior=l_ant
                ))

        if not tem_251:
            novos_objetos.append(MedidorBaseV4(
                ordem=569, bloco="B", andar="2º",
                unidade="B - 2º - 251", unidade_curta="251", unidade_raw="B - 251", tipo="SEM_MEDIDOR",
                hidrometro="-", medidor_visual="-", medidor_dna="SEM_MED_251_1",
                fracao=0.002751, taxa=0.0, l_anterior=0.0, data_anterior="-"
            ))

        db.session.bulk_save_objects(novos_objetos)
        db.session.commit()
        return len(novos_objetos)

    @staticmethod
    def executar_fechamento(info_arq, modo_leitura, dados_caesb):
        val_caesb_res = float(dados_caesb.get('val_res', 62562.80))
        val_caesb_com = float(dados_caesb.get('val_com', 6452.54))
        m3_caesb_res = float(dados_caesb.get('m3_res', 5248))
        m3_caesb_com = float(dados_caesb.get('m3_com', 162))

        # Se a Base Fixa ainda tiver poucos registros (apenas o seed inicial) ou se o usuário subiu uma Planilha Oficial, atualiza a Base Fixa automaticamente!
        medidores_count = MedidorBaseV4.query.count()
        if medidores_count < 100 or info_arq['tipo_arquivo'] in ['PLANILHA_6ABAS_COBRANCA', 'PLANILHA_9ABAS_UNDS']:
            MotorFaturamento.importar_ou_atualizar_base_fixa(info_arq)

        medidores_db = MedidorBaseV4.query.order_by(MedidorBaseV4.ordem, MedidorBaseV4.id).all()

        if info_arq['tipo_arquivo'] in ['PLANILHA_6ABAS_COBRANCA', 'PLANILHA_9ABAS_UNDS']:
            raw_df = info_arq['df']
            start_r = 56 if info_arq['tipo_arquivo'] == 'PLANILHA_6ABAS_COBRANCA' else 6
            rows_extracted = []
            for idx in range(start_r, len(raw_df)):
                r = raw_df.iloc[idx]
                if pd.isna(r[2]): continue
                rows_extracted.append({
                    'LOCAL': str(r[2]),
                    'TIPO': str(r[3]) if pd.notna(r[3]) else '-',
                    'ID_MEDIDOR': str(r[4]).split('.')[0] if pd.notna(r[4]) else '-',
                    'INDICE_ANTIGO': r[6],
                    'DATA_LEITURA': str(r[7]) if pd.notna(r[7]) else '06/10/2026',
                    'LEITURA': r[8]
                })
            df_mes = pd.DataFrame(rows_extracted)
        else:
            df_mes = info_arq['df'].copy()

        # Limpeza segura de nomes de colunas
        cols_orig = list(df_mes.columns)
        cols_clean = {}
        for c in cols_orig:
            cl = MotorFaturamento.limpa_nome_coluna(c)
            if cl and cl not in cols_clean:
                cols_clean[cl] = c

        # Busca flexível por substring (resolve N°. do medidor -> NODOMEDIDOR)
        col_ordem = next((cols_clean[k] for k in cols_clean if 'ORDEM' in k), None)
        col_loc = next((cols_clean[k] for k in cols_clean if 'LOCAL' in k or 'UNIDADE' in k), None)
        col_hid = next((cols_clean[k] for k in cols_clean if k in ['TIPO', 'DESCRICAO', 'HIDROMETRO'] or 'HIDRO' in k or 'DESCRIC' in k), None)
        col_med = next((cols_clean[k] for k in cols_clean if 'MEDIDOR' in k), None)
        col_ant = next((cols_clean[k] for k in cols_clean if 'ANTIGO' in k or 'ANTERIOR' in k), None)
        col_atu = next((cols_clean[k] for k in cols_clean if k in ['INDICE', 'LEITURA', 'LEITURA_ATU', 'LEITURAATU'] or ('INDICE' in k and 'ANTIGO' not in k) or ('LEITURA' in k and 'DATA' not in k and 'ANT' not in k)), None)
        col_data = next((cols_clean[k] for k in cols_clean if 'DATA' in k), None)

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
            raise ValueError(f"Colunas de Medidor ou Leitura Atual não encontradas. Colunas do arquivo: {cols_orig[:8]}")

        registros_mes = {}
        for idx_r, row in df_mes.iterrows():
            dna = MotorFaturamento.limpar_id_medidor(row[col_med])
            if not dna or dna == '-': continue
            l_atu = int(round(MotorFaturamento.limpar_num(row[col_atu], 0.0)))
            l_ant = int(round(MotorFaturamento.limpar_num(row[col_ant], -1.0))) if col_ant and pd.notna(row[col_ant]) and str(row[col_ant]).strip() != '' else -1
            dt_str = str(row[col_data]).strip().split(' ')[0] if col_data and pd.notna(row[col_data]) else "06/10/2026"
            ord_arq = int(MotorFaturamento.limpar_num(row[col_ordem], idx_r + 1)) if col_ordem else (idx_r + 1)
            loc_arq = str(row[col_loc]).strip() if col_loc and pd.notna(row[col_loc]) else ""
            hid_arq = str(row[col_hid]).strip() if col_hid and pd.notna(row[col_hid]) and str(row[col_hid]).strip().upper() not in ['NAN', 'NONE', ''] else ""

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
            if modo_leitura == 'MIZAR' and ('/10/' not in dt_str and '-10-' not in dt_str):
                alm['sem_transmissao'] = 1

            registros_mes[dna] = {
                'l_atu': l_atu, 'l_ant': l_ant, 'data_atu': dt_str,
                'ordem_arq': ord_arq, 'loc_arq': loc_arq, 'hid_arq': hid_arq,
                'alarmes': alm
            }

        # Atualiza Hidrômetros e Leituras Anteriores em lote se vierem no arquivo
        atualizou_db = False
        with db.session.no_autoflush:
            for m_obj in medidores_db:
                info_m = registros_mes.get(m_obj.medidor_dna)
                if info_m:
                    if info_m['l_ant'] >= 0 and m_obj.l_anterior != info_m['l_ant']:
                        m_obj.l_anterior = float(info_m['l_ant'])
                        atualizou_db = True
                    if info_m['hid_arq'] and m_obj.hidrometro in ['', '-', None]:
                        m_obj.hidrometro = info_m['hid_arq']
                        atualizou_db = True
        if atualizou_db:
            db.session.commit()

        linhas_proc = []
        for cad in medidores_db:
            dna = cad.medidor_dna
            tipo = cad.tipo
            l_ant_final = int(round(float(cad.l_anterior)))
            dt_ant_final = cad.data_anterior or "04/09/2026"

            if tipo == 'SEM_MEDIDOR' or dna.startswith('SEM_MED'):
                linhas_proc.append({
                    'ORDEM': int(cad.ordem), 'BLOCO': cad.bloco or 'B', 'ANDAR': cad.andar or '2º',
                    'UNIDADE': cad.unidade, 'UNIDADE_CURTA': cad.unidade_curta or '251',
                    'UNIDADE_RAW': cad.unidade_raw, 'TIPO': 'SEM_MEDIDOR',
                    'HIDROMETRO': '-', 'MEDIDOR_VISUAL': '-', 'MEDIDOR_DNA': dna,
                    'DATA_ANT': '-', 'L_ANTERIOR': 0, 'DATA_LEITURA': '-', 'VAL_ATU': 0,
                    'CONSUMO': 0, 'FRACAO': float(cad.fracao), 'TAXA': 0.0,
                    'ALARMES': {'impossivel':0,'desmontagem':0,'vazamento':0,'fraude':0,'bloqueado':0,'subvazao':0,'retorno':0,'sem_transmissao':0}
                })
                continue

            info_m = registros_mes.get(dna, None)
            hidro_final = (info_m['hid_arq'] if info_m and info_m['hid_arq'] else cad.hidrometro) or '-'
            if info_m:
                if info_m['l_ant'] >= 0:
                    l_ant_final = info_m['l_ant']
                l_atu_final = info_m['l_atu']
                dt_atu_final = info_m['data_atu']
                alm_final = info_m['alarmes']
            else:
                l_atu_final = l_ant_final
                dt_atu_final = "N/A"
                alm_final = {'impossivel':0,'desmontagem':0,'vazamento':0,'fraude':0,'bloqueado':0,'subvazao':0,'retorno':0,'sem_transmissao':1}

            # Virada de Relógio (Ex: D - 1º - 139, medidor 23003778: 999995 -> 2 = 7 m³)
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
                'ORDEM': int(cad.ordem), 'BLOCO': cad.bloco or '-', 'ANDAR': cad.andar or '-',
                'UNIDADE': cad.unidade, 'UNIDADE_CURTA': cad.unidade_curta or cad.unidade,
                'UNIDADE_RAW': cad.unidade_raw, 'TIPO': tipo,
                'HIDROMETRO': hidro_final, 'MEDIDOR_VISUAL': cad.medidor_visual, 'MEDIDOR_DNA': dna,
                'DATA_ANT': dt_ant_final, 'L_ANTERIOR': l_ant_final, 'DATA_LEITURA': dt_atu_final, 'VAL_ATU': l_atu_exib,
                'CONSUMO': consumo, 'FRACAO': float(cad.fracao), 'TAXA': float(cad.taxa),
                'ALARMES': alm_final
            })

        df = pd.DataFrame(linhas_proc)

        # 1. CÁLCULO COMERCIAL (5 Unidades = 4 Lojas Diretas + Lavanderia OMO)
        mask_com_todos = df['TIPO'].isin(['COMERCIAL', 'COMERCIAL_OMO'])
        total_m3_com = float(df.loc[mask_com_todos, 'CONSUMO'].sum())
        preco_m3_com = (val_caesb_com / total_m3_com) if total_m3_com > 0 else 0.0

        df['FIXA_AGUA'] = 0.0
        df['FIXA_ESGOTO'] = 0.0
        df['V_AGUA'] = 0.0
        df['V_ESGOTO'] = 0.0

        df.loc[mask_com_todos, 'V_AGUA'] = df.loc[mask_com_todos, 'CONSUMO'] * preco_m3_com

        # 2. CÁLCULO RESIDENCIAL, EMPÓRIO, UNIDADE 251 E ÁREAS COMUNS
        mask_res_padrao = df['TIPO'] == 'RESIDENCIAL'
        mask_sem_med = df['TIPO'] == 'SEM_MEDIDOR'
        mask_emporio = df['TIPO'] == 'EMPORIO'
        mask_area_comum = df['TIPO'] == 'AREA_COMUM'

        mask_recebe_fixa = (mask_res_padrao | mask_sem_med) & (~df.duplicated(subset=['UNIDADE']))
        df.loc[mask_recebe_fixa, 'FIXA_AGUA'] = 11.62
        df.loc[mask_recebe_fixa, 'FIXA_ESGOTO'] = 11.62

        mask_cascata = mask_res_padrao | mask_emporio | mask_area_comum
        df.loc[mask_cascata, 'V_AGUA'] = df.loc[mask_cascata, 'CONSUMO'].apply(MotorFaturamento.calcular_caesb_cascata)
        df.loc[mask_cascata, 'V_ESGOTO'] = df.loc[mask_cascata, 'V_AGUA']

        df['SUBTOTAL_LINHA'] = df['FIXA_AGUA'] + df['FIXA_ESGOTO'] + df['V_AGUA'] + df['V_ESGOTO']

        # 3. FECHAMENTO DO RATEIO POR FRAÇÃO IDEAL
        mask_bloco_res = df['TIPO'].isin(['RESIDENCIAL', 'EMPORIO', 'SEM_MEDIDOR'])
        mask_bloco_comum = df['TIPO'].isin(['AREA_COMUM', 'COMERCIAL_OMO'])

        consumo_rs_unidades = float(df.loc[mask_bloco_res, 'SUBTOTAL_LINHA'].sum())
        taxa_leitura_unidades = float(df.loc[mask_bloco_res, 'TAXA'].sum())

        v_agua_comum_total = float(df.loc[mask_bloco_comum, 'V_AGUA'].sum())
        v_esgoto_comum_total = v_agua_comum_total
        consumo_rs_comum = v_agua_comum_total + v_esgoto_comum_total
        taxa_leitura_comum = float(df.loc[mask_bloco_comum, 'TAXA'].sum())

        taxa_leitura_total = taxa_leitura_unidades + taxa_leitura_comum
        despesa_total_condominio = val_caesb_res + taxa_leitura_total
        arrecadacao_unidades_base = consumo_rs_unidades + taxa_leitura_unidades
        diferenca_caesb = (consumo_rs_unidades + consumo_rs_comum) - val_caesb_res

        valor_a_ratear = despesa_total_condominio - arrecadacao_unidades_base

        df['FRACAO_UNICA'] = 0.0
        mask_primeira_linha_res = mask_bloco_res & (~df.duplicated(subset=['UNIDADE']))
        df.loc[mask_primeira_linha_res, 'FRACAO_UNICA'] = df.loc[mask_primeira_linha_res, 'FRACAO']

        df['VAL_RATEIO'] = df['FRACAO_UNICA'] * valor_a_ratear

        somas_unidade = df.groupby('UNIDADE').agg(
            SUBTOTAL_UNIDADE=('SUBTOTAL_LINHA', 'sum'),
            RATEIO_UNIDADE=('VAL_RATEIO', 'sum'),
            TAXA_UNIDADE=('TAXA', 'sum'),
            CONSUMO_UNIDADE=('CONSUMO', 'sum'),
            QTD_MEDIDORES=('MEDIDOR_DNA', lambda x: sum(1 for i in x if not str(i).startswith('SEM_MED'))),
            HIDROMETROS_STR=('HIDROMETRO', lambda x: " / ".join([str(v) for v in x if str(v).strip() not in ['', '-']]) or '-'),
            MEDIDORES_STR=('MEDIDOR_VISUAL', lambda x: " / ".join([str(v) for v in x if str(v).strip() not in ['', '-']]) or '-')
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

        # 4. CONSOLIDAÇÃO POR UNIDADE (ABA COBRANÇA / ÂNCORA)
        unidades_consolidadas = []
        ordem_unidades = df.drop_duplicates(subset=['UNIDADE'])[['ORDEM', 'BLOCO', 'ANDAR', 'UNIDADE', 'UNIDADE_CURTA', 'TIPO', 'FRACAO_UNICA']]
        for _, u_row in ordem_unidades.iterrows():
            if u_row['TIPO'] in ['AREA_COMUM', 'COMERCIAL_OMO']:
                continue
            u_nome = u_row['UNIDADE']
            info_s = mapa_sub[u_nome]
            unidades_consolidadas.append({
                'ORDEM': int(u_row['ORDEM']),
                'BLOCO': u_row['BLOCO'] or '-',
                'ANDAR': u_row['ANDAR'] or '-',
                'UNIDADE': u_nome,
                'UNIDADE_CURTA': u_row['UNIDADE_CURTA'] or u_nome,
                'TIPO': u_row['TIPO'],
                'HIDROMETROS': info_s['HIDROMETROS_STR'],
                'MEDIDORES': info_s['MEDIDORES_STR'],
                'QTD_MED': int(info_s['QTD_MEDIDORES']),
                'FRACAO': float(u_row['FRACAO_UNICA']),
                'CONSUMO': float(info_s['CONSUMO_UNIDADE']),
                'SUBTOTAL': float(info_s['SUBTOTAL_UNIDADE']),
                'VAL_RATEIO': float(info_s['RATEIO_UNIDADE']),
                'TAXA': float(info_s['TAXA_UNIDADE']),
                'TOTAL': float(info_s['TOTAL_UNIDADE'])
            })

        # 5. ALARMES
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
            'caesb_detalhes': dados_caesb,
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

with app.app_context():
    db.create_all()
    MotorFaturamento.inicializar_base_padrao_se_vazia()


# ==============================================================================
# INTERFACE WEB ENTERPRISE (TABELA E COLUNAS SEMPRE VISÍVEIS AO ABRIR)
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
    <script src="https://cdn.jsdelivr.net/npm/tesseract.js@5/dist/tesseract.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
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
            padding: 16px;
            min-height: 100vh;
        }
        .container { max-width: 1620px; margin: auto; }

        /* 1. BARRA SUPERIOR DE MÓDULOS */
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

        /* 2. SUB-ABAS E STATUS DA BASE FIXA */
        .sub-nav-wrap {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 10px;
            margin-bottom: 16px;
        }
        .sub-nav { display: flex; gap: 10px; flex-wrap: wrap; }
        .sub-tab {
            background: #111c2d; border: 1px solid var(--panel-border);
            color: var(--text-muted); padding: 10px 20px; border-radius: 8px;
            cursor: pointer; font-weight: 700; font-size: 13px;
            display: flex; align-items: center; gap: 8px; transition: 0.2s;
        }
        .sub-tab:hover { color: white; border-color: #3b82f6; }
        .sub-tab.active-sub-mizar { background: var(--mizar-blue); color: white; border-color: var(--mizar-blue); }
        .sub-tab.active-sub-walkby { background: var(--walkby-green); color: white; border-color: var(--walkby-green); }

        .base-fixa-bar {
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(16, 185, 129, 0.12);
            border: 1px solid rgba(16, 185, 129, 0.4);
            padding: 6px 14px;
            border-radius: 8px;
            font-size: 12px;
            font-weight: 700;
            color: #34d399;
        }

        /* 3. CARDS KPI */
        .kpi-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 14px;
            margin-bottom: 16px;
        }
        .kpi-card {
            background: var(--panel-bg);
            border: 1px solid var(--panel-border);
            border-radius: 12px;
            padding: 16px 18px;
            display: flex;
            align-items: center;
            gap: 14px;
            box-shadow: 0 6px 15px rgba(0,0,0,0.25);
        }
        .kpi-icon {
            width: 50px; height: 50px; border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            font-size: 22px; flex-shrink: 0;
        }
        .icon-blue { background: rgba(26, 115, 232, 0.2); color: #38bdf8; border: 1px solid rgba(56,189,248,0.3); }
        .icon-amber { background: rgba(217, 119, 6, 0.2); color: #fbbf24; border: 1px solid rgba(251,191,36,0.3); }
        .icon-indigo { background: rgba(99, 102, 241, 0.2); color: #818cf8; border: 1px solid rgba(129,140,248,0.3); }
        .icon-red { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(248,113,113,0.3); }
        .kpi-info h4 { margin: 0 0 5px 0; font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.6px; }
        .kpi-info h2 { margin: 0; font-size: 24px; font-weight: 800; }

        /* 4. PAINEL DE FATURAS CAESB */
        .caesb-panel {
            background: var(--panel-bg);
            border: 1px solid var(--panel-border);
            border-radius: 12px;
            padding: 16px 20px;
            margin-bottom: 16px;
        }
        .caesb-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
            margin-bottom: 14px;
            padding-bottom: 10px;
            border-bottom: 1px solid var(--panel-border);
        }
        .caesb-title {
            font-size: 14px;
            font-weight: 800;
            color: #38bdf8;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .caesb-grid {
            display: grid;
            grid-template-columns: repeat(10, 1fr);
            gap: 10px;
            margin-bottom: 14px;
        }
        @media (max-width: 1300px) {
            .caesb-grid { grid-template-columns: repeat(5, 1fr); }
        }
        .field-box { display: flex; flex-direction: column; gap: 4px; }
        .field-box label { font-size: 11px; color: var(--text-muted); font-weight: 700; white-space: nowrap; }
        .field-box input {
            background: #09101d;
            border: 1px solid #10b98160;
            color: #ecfdf5;
            padding: 8px 10px;
            border-radius: 7px;
            font-weight: 700;
            font-size: 13px;
            width: 100%;
            text-align: center;
        }
        .field-box input.highlight-green {
            border-color: #10b981;
            background: rgba(16, 185, 129, 0.12);
            color: #34d399;
        }
        .field-box input.highlight-amber {
            border-color: #f59e0b;
            background: rgba(245, 158, 11, 0.12);
            color: #fbbf24;
        }

        .btn-group { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
        button {
            padding: 10px 16px; border: none; border-radius: 8px;
            font-weight: 700; font-size: 13px; cursor: pointer;
            color: white; display: flex; align-items: center; gap: 8px;
            transition: 0.2s;
        }
        button:hover { filter: brightness(1.15); transform: translateY(-1px); }
        .btn-ocr-res { background: linear-gradient(135deg, #0284c7, #0369a1); border: 1px solid #38bdf860; }
        .btn-ocr-com { background: linear-gradient(135deg, #b45309, #92400e); border: 1px solid #fbbf2460; }
        .btn-gold { background: linear-gradient(135deg, #d97706, #b45309); }
        .btn-mizar { background: linear-gradient(135deg, #2563eb, #1d4ed8); }
        .btn-walkby { background: linear-gradient(135deg, #10b981, #059669); }
        .btn-subtle { background: #1e293b; border: 1px solid #334155; color: #cbd5e1; padding: 7px 12px; font-size: 12px; }
        .btn-excel { background: linear-gradient(135deg, #16a34a, #15803d); }
        .btn-pdf { background: linear-gradient(135deg, #dc2626, #b91c1c); }

        /* 5. BARRA DE PESQUISA E EXPORTAÇÃO */
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
            width: 100%; padding: 10px 14px; border-radius: 8px;
            border: 1px solid #334155; background: #0b1322; color: white; font-size: 14px;
        }

        /* 6. TABELAS E CONTEÚDO */
        .content-box {
            background: var(--panel-bg);
            border: 1px solid var(--panel-border);
            border-radius: 12px;
            padding: 16px;
            overflow-x: auto;
            max-height: 640px;
        }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th {
            background: #1a2942; color: #e2e8f0; padding: 11px 8px;
            position: sticky; top: 0; z-index: 10; text-align: center;
            font-weight: 700; border-bottom: 2px solid #2d4263;
        }
        td { padding: 9px 8px; text-align: center; border-bottom: 1px solid #1e293b; }
        tr:hover { background: rgba(59, 130, 246, 0.08); }

        .hidro-badge {
            color: #38bdf8;
            font-family: 'Consolas', 'Courier New', monospace;
            font-weight: 700;
            background: rgba(56, 189, 248, 0.1);
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 12px;
        }
        .med-badge {
            color: #f8fafc;
            font-family: 'Consolas', 'Courier New', monospace;
            font-weight: 700;
            font-size: 12.5px;
        }

        .tag-res { color: #38bdf8; font-weight: 700; }
        .tag-com { color: #fbbf24; font-weight: 700; }
        .tag-emp { color: #a78bfa; font-weight: 700; }
        .tag-are { color: #34d399; font-weight: 700; }
        .tag-sem { color: #f472b6; font-weight: 700; }

        .exec-section { margin-bottom: 22px; }
        .exec-title {
            background: linear-gradient(90deg, #1e3a8a, #0f172a);
            padding: 9px 15px; border-radius: 8px; font-weight: 800;
            font-size: 13px; letter-spacing: 0.5px; margin-bottom: 10px;
            border-left: 4px solid #38bdf8;
        }
        .exec-title.orange { background: linear-gradient(90deg, #7c2d12, #0f172a); border-left-color: #fbbf24; }
        .exec-title.green { background: linear-gradient(90deg, #065f46, #0f172a); border-left-color: #34d399; }

        #loading {
            display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(7, 12, 22, 0.88); z-index: 9999;
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
    <h3 id="loading-msg" style="margin-top: 18px; color: #38bdf8; text-align:center; max-width:520px;">Processando...</h3>
</div>

<div class="container">
    <!-- 1. MÓDULOS PRINCIPAIS -->
    <div class="top-modules">
        <div id="mod-MIZAR" class="module-btn active-mizar" onclick="selecionarModulo('MIZAR')">
            <div class="module-left">
                <span class="badge-num">1</span>
                <span>💧 MIZAR (Telemetria)</span>
            </div>
            <span id="status-mizar" class="status-pill">Base Fixa Pronta</span>
        </div>
        <div id="mod-WALKBY" class="module-btn" onclick="selecionarModulo('WALKBY')">
            <div class="module-left">
                <span class="badge-num">2</span>
                <span>🚶 WALK-BY (Coleta Antena)</span>
            </div>
            <span id="status-walkby" class="status-pill">Base Fixa Pronta</span>
        </div>
        <div id="mod-COMPARATIVO" class="module-btn" onclick="selecionarModulo('COMPARATIVO')">
            <div class="module-left">
                <span class="badge-num">3</span>
                <span>⚖️ COMPARATIVO</span>
            </div>
            <span class="status-pill">Mizar vs Walk-by</span>
        </div>
    </div>

    <!-- 2. SUB-ABAS DE NAVEGAÇÃO + INDICADOR DE BASE FIXA -->
    <div class="sub-nav-wrap">
        <div class="sub-nav" id="sub-nav-bar">
            <button id="sub-UNIDADES" class="sub-tab active-sub-mizar" onclick="selecionarSubAba('UNIDADES')">🏢 UNIDADES (Hidrômetro + Medidor)</button>
            <button id="sub-GERAL" class="sub-tab" onclick="selecionarSubAba('GERAL')">🏠 GERAL (Demonstrativo)</button>
            <button id="sub-COBRANCA" class="sub-tab" onclick="selecionarSubAba('COBRANCA')">💲 COBRANÇA (Âncora Consolidado)</button>
            <button id="sub-ALARMES" class="sub-tab" onclick="selecionarSubAba('ALARMES')">🔔 ALARMES & AUDITORIA</button>
        </div>

        <div class="base-fixa-bar">
            <span id="lbl-base-fixa">🔒 Base Fixa Permanente Ativa</span>
            <button class="btn-subtle" onclick="virarMesBaseFixa()" title="Salva as leituras atuais como Leitura Anterior para o próximo mês">🔄 Virar Mês</button>
        </div>
    </div>

    <!-- 3. CARDS KPI -->
    <div class="kpi-grid">
        <div class="kpi-card">
            <div class="kpi-icon icon-blue">💧</div>
            <div class="kpi-info">
                <h4 id="kpi1-title">CONSUMO MEDIDO UNIDS</h4>
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
                <h4 id="kpi3-title">FATURA COMERCIAL</h4>
                <h2 id="lbl-arr-com" style="color:#fbbf24;">R$ 6.452,54</h2>
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

    <!-- 4. PAINEL INTELIGENTE DA CONTA CAESB (10 CAMPOS VERDES + OCR PDF/JPEG) -->
    <div class="caesb-panel">
        <div class="caesb-header">
            <div class="caesb-title">
                <span>🧾 DADOS OFICIAIS DAS CONTAS CAESB (PREENCHIMENTO AUTOMÁTICO VIA PDF OU FOTO JPEG/PNG)</span>
            </div>
            <div class="btn-group">
                <input type="file" id="file_conta_res" accept=".pdf,.jpg,.jpeg,.png" style="display:none" onchange="importarContaCAESB('RES')">
                <button class="btn-ocr-res" onclick="document.getElementById('file_conta_res').click()">📄 Importar Conta CAESB Residencial (PDF / JPEG)</button>

                <input type="file" id="file_conta_com" accept=".pdf,.jpg,.jpeg,.png" style="display:none" onchange="importarContaCAESB('COM')">
                <button class="btn-ocr-com" onclick="document.getElementById('file_conta_com').click()">📄 Importar Conta CAESB Comercial (PDF / JPEG)</button>
            </div>
        </div>

        <!-- LINHA 1: 10 CAMPOS DA CONTA CAESB RESIDENCIAL -->
        <div style="font-size:12px; font-weight:800; color:#34d399; margin-bottom:6px;">💧 CONTA CAESB — CATEGORIA RESIDENCIAL (HIDRÔMETRO EXTERNO GERAL)</div>
        <div class="caesb-grid">
            <div class="field-box">
                <label>Data Leit. Ant.</label>
                <input type="text" id="res_dt_ant" value="03/09/2026">
            </div>
            <div class="field-box">
                <label>Leitura Anterior</label>
                <input type="number" id="res_l_ant" value="411389" oninput="recalcularSomaCAESB('res')">
            </div>
            <div class="field-box">
                <label>Data Leit. Atual</label>
                <input type="text" id="res_dt_atu" value="06/10/2026">
            </div>
            <div class="field-box">
                <label>Leitura Atual</label>
                <input type="number" id="res_l_atu" value="416637" oninput="recalcularSomaCAESB('res')">
            </div>
            <div class="field-box">
                <label>Volume Medido (m³)</label>
                <input type="number" id="m3_res" value="5248" class="highlight-green">
            </div>
            <div class="field-box">
                <label>Fixa Água (R$)</label>
                <input type="number" id="res_fixa_agua" value="8715.00" step="0.01" oninput="recalcularSomaCAESB('res')">
            </div>
            <div class="field-box">
                <label>Fixa Esgoto (R$)</label>
                <input type="number" id="res_fixa_esg" value="8715.00" step="0.01" oninput="recalcularSomaCAESB('res')">
            </div>
            <div class="field-box">
                <label>Variável Água (R$)</label>
                <input type="number" id="res_var_agua" value="22566.40" step="0.01" oninput="recalcularSomaCAESB('res')">
            </div>
            <div class="field-box">
                <label>Variável Esgoto (R$)</label>
                <input type="number" id="res_var_esg" value="22566.40" step="0.01" oninput="recalcularSomaCAESB('res')">
            </div>
            <div class="field-box">
                <label>TOTAL CAESB RES (R$)</label>
                <input type="number" id="val_res" value="62562.80" step="0.01" class="highlight-green">
            </div>
        </div>

        <!-- LINHA 2: 10 CAMPOS DA CONTA CAESB COMERCIAL -->
        <div style="font-size:12px; font-weight:800; color:#fbbf24; margin-bottom:6px;">🏢 CONTA CAESB — CATEGORIA COMERCIAL (LOJAS / MEDIDOR EXTERNO)</div>
        <div class="caesb-grid" style="margin-bottom: 16px;">
            <div class="field-box">
                <label>Data Leit. Ant.</label>
                <input type="text" id="com_dt_ant" value="03/09/2026">
            </div>
            <div class="field-box">
                <label>Leitura Anterior</label>
                <input type="number" id="com_l_ant" value="1465" oninput="recalcularSomaCAESB('com')">
            </div>
            <div class="field-box">
                <label>Data Leit. Atual</label>
                <input type="text" id="com_dt_atu" value="06/10/2026">
            </div>
            <div class="field-box">
                <label>Leitura Atual</label>
                <input type="number" id="com_l_atu" value="1627" oninput="recalcularSomaCAESB('com')">
            </div>
            <div class="field-box">
                <label>Volume Medido (m³)</label>
                <input type="number" id="m3_com" value="162" class="highlight-amber">
            </div>
            <div class="field-box">
                <label>Fixa Água (R$)</label>
                <input type="number" id="com_fixa_agua" value="30.51" step="0.01" oninput="recalcularSomaCAESB('com')">
            </div>
            <div class="field-box">
                <label>Fixa Esgoto (R$)</label>
                <input type="number" id="com_fixa_esg" value="30.51" step="0.01" oninput="recalcularSomaCAESB('com')">
            </div>
            <div class="field-box">
                <label>Variável Água (R$)</label>
                <input type="number" id="com_var_agua" value="3195.76" step="0.01" oninput="recalcularSomaCAESB('com')">
            </div>
            <div class="field-box">
                <label>Variável Esgoto (R$)</label>
                <input type="number" id="com_var_esg" value="3195.76" step="0.01" oninput="recalcularSomaCAESB('com')">
            </div>
            <div class="field-box">
                <label>TOTAL CAESB COM (R$)</label>
                <input type="number" id="val_com" value="6452.54" step="0.01" class="highlight-amber">
            </div>
        </div>

        <!-- BOTÕES PRINCIPAIS DE IMPORTAÇÃO E PROCESSAMENTO -->
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; padding-top:10px; border-top:1px solid var(--panel-border);">
            <span style="font-size:12px; color:#34d399; font-weight:600;">
                ✅ Você pode enviar a planilha ou TXT direto nos botões abaixo (gravação ultra-rápida em 0,2s no banco):
            </span>
            <div class="btn-group">
                <input type="file" id="file_base" accept=".xlsx,.xls,.csv,.txt" style="display:none" onchange="uploadBase()">
                <button class="btn-gold" onclick="document.getElementById('file_base').click()">📁 1. Carregar / Fixar Dicionário Base</button>

                <input type="file" id="file_mizar" accept=".csv,.txt,.xlsx,.xls" style="display:none" onchange="processarArquivo('MIZAR')">
                <button class="btn-mizar" onclick="document.getElementById('file_mizar').click()">💧 2. Processar MIZAR (.csv / .xlsx)</button>

                <input type="file" id="file_walkby" accept=".txt,.csv,.xlsx,.xls" style="display:none" onchange="processarArquivo('WALKBY')">
                <button class="btn-walkby" onclick="document.getElementById('file_walkby').click()">🚶 3. Processar WALK-BY (.txt / .xlsx)</button>
            </div>
        </div>
    </div>

    <!-- 5. BARRA DE PESQUISA E EXPORTAÇÕES -->
    <div class="search-panel">
        <div class="search-box">
            <label>🔍 Procurar por Hidrômetro (ex: A23B182990), Medidor (ex: 23419692), Unidade ou Alarme:</label>
            <input type="text" id="searchInput" placeholder="Digite o código do Hidrômetro, número do Medidor, Unidade (ex: 251, KITCLEAN)..." onkeyup="filtrarTabelaVisivel()">
        </div>
        <div class="btn-group">
            <button class="btn-excel" onclick="exportarExcelCompleto()">📊 Exportar Excel (Todas as Abas)</button>
            <button class="btn-pdf" onclick="exportarPDFExecutivo()">📄 Emitir PDF Gerencial Resumido</button>
        </div>
    </div>

    <!-- 6. TABELA SEMPRE VISÍVEL COM TODAS AS COLUNAS (INCLUINDO HIDRÔMETRO + MEDIDOR) -->
    <div class="content-box" id="main-content">
        <table id="dataTable" class="filterable-table">
            <thead>
                <tr>
                    <th>Bloco</th><th>Andar</th><th>Unidade</th><th>Tipo</th>
                    <th>Hidrômetro</th><th>Medidor</th><th>Data Leit.</th>
                    <th>L. Ant.</th><th>L. Atual</th><th>Consumo (m³)</th>
                    <th>Fixa Água</th><th>Fixa Esg.</th><th>Var. Água</th><th>Var. Esg.</th>
                    <th>Subtotal Unid.</th><th>Rateio Fração</th><th>Taxa</th><th>TOTAL UNID.</th>
                </tr>
            </thead>
            <tbody id="tbody-inicial">
                <tr><td colspan="18" style="padding:25px;">Carregando Base Fixa de Hidrômetros e Medidores...</td></tr>
            </tbody>
        </table>
    </div>
</div>

<script>
    if (window.pdfjsLib) {
        window.pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
    }

    let estado = {
        moduloAtivo: 'MIZAR',
        subAbaAtiva: 'UNIDADES',
        baseFixaPreview: [],
        MIZAR: null,
        WALKBY: null
    };

    const fmtBRL = (v) => "R$ " + Number(v || 0).toLocaleString('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    const fmtNum = (v, d=0) => Number(v || 0).toLocaleString('pt-BR', {minimumFractionDigits: d, maximumFractionDigits: d});

    function showLoading(show, msg="Processando Matemática Mestra...") {
        document.getElementById('loading-msg').innerText = msg;
        document.getElementById('loading').style.display = show ? 'flex' : 'none';
    }

    function recalcularSomaCAESB(pref) {
        const lAnt = parseInt(document.getElementById(`${pref}_l_ant`).value) || 0;
        const lAtu = parseInt(document.getElementById(`${pref}_l_atu`).value) || 0;
        if (lAtu >= lAnt && lAtu > 0) {
            document.getElementById(`m3_${pref}`).value = lAtu - lAnt;
        }
        const fa = parseFloat(document.getElementById(`${pref}_fixa_agua`).value) || 0;
        const fe = parseFloat(document.getElementById(`${pref}_fixa_esg`).value) || 0;
        const va = parseFloat(document.getElementById(`${pref}_var_agua`).value) || 0;
        const ve = parseFloat(document.getElementById(`${pref}_var_esg`).value) || 0;
        const soma = fa + fe + va + ve;
        if (soma > 0) {
            document.getElementById(`val_${pref}`).value = soma.toFixed(2);
        }
    }

    async function importarContaCAESB(tipoAlvo) {
        const inputEl = document.getElementById(tipoAlvo === 'RES' ? 'file_conta_res' : 'file_conta_com');
        const file = inputEl.files[0];
        if (!file) return;

        showLoading(true, `Lendo Conta CAESB (${file.name}) via Visão Computacional OCR...`);
        try {
            let textoExtraido = "";
            const fname = file.name.toLowerCase();

            if (fname.endsWith('.pdf')) {
                const arrayBuffer = await file.arrayBuffer();
                const pdf = await window.pdfjsLib.getDocument({ data: arrayBuffer }).promise;
                for (let i = 1; i <= pdf.numPages; i++) {
                    const page = await pdf.getPage(i);
                    const content = await page.getTextContent();
                    const pageText = content.items.map(item => item.str).join(' ');
                    textoExtraido += pageText + "\n";

                    if (pageText.trim().length < 30) {
                        const viewport = page.getViewport({ scale: 2.0 });
                        const canvas = document.createElement('canvas');
                        const ctx = canvas.getContext('2d');
                        canvas.width = viewport.width;
                        canvas.height = viewport.height;
                        await page.render({ canvasContext: ctx, viewport: viewport }).promise;
                        const { data: { text } } = await Tesseract.recognize(canvas, 'por');
                        textoExtraido += text + "\n";
                    }
                }
            } else {
                const { data: { text } } = await Tesseract.recognize(file, 'por');
                textoExtraido = text;
            }

            let req = await fetch('/parse_conta_caesb', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ texto: textoExtraido })
            });
            let res = await req.json();
            showLoading(false);
            inputEl.value = "";

            if (res.error) return alert("Erro ao interpretar fatura: " + res.error);

            const pref = tipoAlvo === 'RES' ? 'res' : 'com';
            if (res.data_ant) document.getElementById(`${pref}_dt_ant`).value = res.data_ant;
            if (res.leitura_ant > 0) document.getElementById(`${pref}_l_ant`).value = res.leitura_ant;
            if (res.data_atu) document.getElementById(`${pref}_dt_atu`).value = res.data_atu;
            if (res.leitura_atu > 0) document.getElementById(`${pref}_l_atu`).value = res.leitura_atu;
            if (res.volume_m3 > 0) document.getElementById(`m3_${pref}`).value = res.volume_m3;
            if (res.fixa_agua > 0) document.getElementById(`${pref}_fixa_agua`).value = res.fixa_agua.toFixed(2);
            if (res.fixa_esgoto > 0) document.getElementById(`${pref}_fixa_esg`).value = res.fixa_esgoto.toFixed(2);
            if (res.var_agua > 0) document.getElementById(`${pref}_var_agua`).value = res.var_agua.toFixed(2);
            if (res.var_esgoto > 0) document.getElementById(`${pref}_var_esg`).value = res.var_esgoto.toFixed(2);
            if (res.total_fatura > 0) document.getElementById(`val_${pref}`).value = res.total_fatura.toFixed(2);

            alert(`✅ Conta CAESB (${tipoAlvo === 'RES' ? 'Residencial' : 'Comercial'}) preenchida automaticamente!\n\n` +
                  `• Período: ${res.data_ant || '-'} a ${res.data_atu || '-'}\n` +
                  `• Leituras: ${res.leitura_ant} -> ${res.leitura_atu} (${res.volume_m3} m³)\n` +
                  `• TOTAL A PAGAR: R$ ${res.total_fatura.toFixed(2)}`);
        } catch (e) {
            showLoading(false);
            inputEl.value = "";
            alert("Erro na leitura do arquivo: " + e);
        }
    }

    function obterDadosCAESBFormulario() {
        return {
            res_dt_ant: document.getElementById('res_dt_ant').value,
            res_l_ant: parseInt(document.getElementById('res_l_ant').value) || 0,
            res_dt_atu: document.getElementById('res_dt_atu').value,
            res_l_atu: parseInt(document.getElementById('res_l_atu').value) || 0,
            m3_res: parseFloat(document.getElementById('m3_res').value) || 0,
            res_fixa_agua: parseFloat(document.getElementById('res_fixa_agua').value) || 0,
            res_fixa_esg: parseFloat(document.getElementById('res_fixa_esg').value) || 0,
            res_var_agua: parseFloat(document.getElementById('res_var_agua').value) || 0,
            res_var_esg: parseFloat(document.getElementById('res_var_esg').value) || 0,
            val_res: parseFloat(document.getElementById('val_res').value) || 0,

            com_dt_ant: document.getElementById('com_dt_ant').value,
            com_l_ant: parseInt(document.getElementById('com_l_ant').value) || 0,
            com_dt_atu: document.getElementById('com_dt_atu').value,
            com_l_atu: parseInt(document.getElementById('com_l_atu').value) || 0,
            m3_com: parseFloat(document.getElementById('m3_com').value) || 0,
            com_fixa_agua: parseFloat(document.getElementById('com_fixa_agua').value) || 0,
            com_fixa_esg: parseFloat(document.getElementById('com_fixa_esg').value) || 0,
            com_var_agua: parseFloat(document.getElementById('com_var_agua').value) || 0,
            com_var_esg: parseFloat(document.getElementById('com_var_esg').value) || 0,
            val_com: parseFloat(document.getElementById('val_com').value) || 0
        };
    }

    window.addEventListener('DOMContentLoaded', async () => {
        try {
            let req = await fetch('/estado_inicial');
            let res = await req.json();
            if (res.base_fixa_preview) {
                estado.baseFixaPreview = res.base_fixa_preview;
            }
            if (res.qtd_base_fixa) {
                document.getElementById('lbl-base-fixa').innerText = `🔒 Base Fixa Ativa (${res.qtd_base_fixa} Medidores)`;
            }
            if (res.MIZAR) {
                estado.MIZAR = res.MIZAR;
                document.getElementById('status-mizar').innerText = "✅ Processado";
                document.getElementById('status-mizar').style.color = "#38bdf8";
            }
            if (res.WALKBY) {
                estado.WALKBY = res.WALKBY;
                document.getElementById('status-walkby').innerText = "✅ Processado";
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
        ['UNIDADES', 'GERAL', 'COBRANCA', 'ALARMES'].forEach(s => {
            document.getElementById('sub-' + s).className = 'sub-tab' + (estado.subAbaAtiva === s ? ' ' + cls : '');
        });
    }

    async function uploadBase() {
        const inputEl = document.getElementById('file_base');
        const file = inputEl.files[0];
        if (!file) return;
        showLoading(true, "Gravando os 1.576 Medidores e Hidrômetros na Base Fixa em alta velocidade...");
        let fd = new FormData();
        fd.append("file", file);
        try {
            let req = await fetch('/upload_base', { method: 'POST', body: fd });
            let res = await req.json();
            showLoading(false);
            inputEl.value = "";
            if (res.error) alert("❌ Erro: " + res.error);
            else {
                estado.baseFixaPreview = res.base_fixa_preview || estado.baseFixaPreview;
                document.getElementById('lbl-base-fixa').innerText = `🔒 Base Fixa Ativa (${res.qtd} Medidores)`;
                renderizarTudo();
                alert("✅ " + res.message);
            }
        } catch (e) {
            showLoading(false);
            inputEl.value = "";
            alert("Erro na comunicação com o servidor: " + e);
        }
    }

    async function virarMesBaseFixa() {
        const modoRef = estado.WALKBY ? 'WALKBY' : (estado.MIZAR ? 'MIZAR' : null);
        if (!modoRef) return alert("Processe o mês atual primeiro antes de virar o ciclo!");
        if (!confirm(`Deseja salvar as Leituras Atuais do fechamento ${modoRef} como as novas Leituras Anteriores na Base Fixa para o próximo mês?`)) return;

        showLoading(true, "Virando o mês e gravando Leituras Atuais na Base Fixa...");
        try {
            let req = await fetch('/virar_mes', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ modo: modoRef })
            });
            let res = await req.json();
            showLoading(false);
            if (res.error) alert("❌ " + res.error);
            else alert("✅ " + res.message);
        } catch (e) {
            showLoading(false);
            alert("Erro ao virar o mês.");
        }
    }

    async function processarArquivo(modo) {
        const inputId = modo === 'MIZAR' ? 'file_mizar' : 'file_walkby';
        const inputEl = document.getElementById(inputId);
        const file = inputEl.files[0];
        if (!file) return;

        showLoading(true, `Calculando Fechamento ${modo} com Base Fixa (Hidrômetro + Medidor + Rateio)...`);
        let fd = new FormData();
        fd.append("file", file);
        fd.append("modo", modo);
        fd.append("dados_caesb", JSON.stringify(obterDadosCAESBFormulario()));

        try {
            let req = await fetch('/processar', { method: 'POST', body: fd });
            let res = await req.json();
            showLoading(false);
            inputEl.value = "";
            if (res.error) return alert("❌ Erro: " + res.error);

            estado[modo] = res;
            estado.baseFixaPreview = res.unidades_hidrometros;
            document.getElementById('lbl-base-fixa').innerText = `🔒 Base Fixa Ativa (${res.unidades_hidrometros.length} Medidores)`;
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
            inputEl.value = "";
            alert("Falha ao processar arquivo no servidor: " + e);
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
            // Mesmo antes de processar o mês, exibe a Tabela Completa com todas as Colunas e a Base Fixa!
            renderizarAbaUnidades(box, { unidades_hidrometros: estado.baseFixaPreview });
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
        const C = R.caesb_detalhes || obterDadosCAESBFormulario();
        const comRows = dadosModo.unidades_hidrometros.filter(x => x.TIPO === 'COMERCIAL' || x.TIPO === 'COMERCIAL_OMO');

        let htmlCom = comRows.map(c => `
            <tr>
                <td style="text-align:left; font-weight:700;">${c.UNIDADE_RAW}</td>
                <td>${c.UNIDADE}</td>
                <td><span class="hidro-badge">${c.HIDROMETRO}</span></td>
                <td><span class="med-badge">${c.MEDIDOR_VISUAL}</span></td>
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
                <div class="exec-title">1. COBRANÇA DA CAESB — RESIDENCIAL & RESUMO DO RATEIO (${R.modo})</div>
                <table style="margin-bottom: 12px;">
                    <thead>
                        <tr>
                            <th>Hidrômetro</th><th>Medidor</th><th>Data Ant.</th><th>Leit. Ant.</th>
                            <th>Data Atual</th><th>Leit. Atual</th><th>Medido (m³)</th>
                            <th>Fixa Água</th><th>Fixa Esgoto</th><th>Var. Água</th><th>Var. Esgoto</th><th>TOTAL FATURA CAESB</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr style="font-weight:700; font-size:13px;">
                            <td><span class="hidro-badge">GERAL</span></td><td><span class="med-badge">EXTERNO</span></td>
                            <td>${C.res_dt_ant || '03/09/2026'}</td><td>${fmtNum(C.res_l_ant || 411389)}</td>
                            <td>${C.res_dt_atu || '06/10/2026'}</td><td>${fmtNum(C.res_l_atu || 416637)}</td>
                            <td style="color:#34d399; font-weight:800;">${fmtNum(R.m3_caesb_res)} m³</td>
                            <td>${fmtBRL(C.res_fixa_agua || 8715)}</td><td>${fmtBRL(C.res_fixa_esg || 8715)}</td>
                            <td>${fmtBRL(C.res_var_agua || 22566.40)}</td><td>${fmtBRL(C.res_var_esg || 22566.40)}</td>
                            <td style="color:#34d399; font-size:15px;">${fmtBRL(R.fatura_caesb_res)}</td>
                        </tr>
                    </tbody>
                </table>

                <table class="filterable-table">
                    <thead>
                        <tr>
                            <th>Consumo Unidades (m³)</th>
                            <th>Consumo Unidades (R$)</th>
                            <th>Tx. Leitura Unidades</th>
                            <th>Consumo Área Comum + OMO (R$)</th>
                            <th>Tx. Leitura Área Comum</th>
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
                            <td style="color:#fbbf24;">${fmtBRL(R.despesa_total_condominio)}</td>
                            <td style="color:#34d399;">${fmtBRL(R.arrecadacao_unidades_base)}</td>
                            <td>${R.status_diferenca} (${fmtBRL(R.diferenca_caesb)})</td>
                            <td style="color:#f87171; font-size:16px;">${fmtBRL(R.valor_a_ratear)}</td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <div class="exec-section">
                <div class="exec-title orange">2. COBRANÇA DA CAESB — COMERCIAL & RATEIO DAS 5 UNIDADES</div>
                <table style="margin-bottom:12px;">
                    <thead>
                        <tr>
                            <th>Data Ant.</th><th>Leit. Ant.</th><th>Data Atual</th><th>Leit. Atual</th>
                            <th>Medido CAESB</th><th>Fixa Água</th><th>Fixa Esgoto</th><th>Var. Água</th><th>Var. Esgoto</th>
                            <th>Fatura Comercial</th><th>Consumo 5 Lojas</th><th>Valor m³ Comercial</th><th>Despesa Total Com.</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr style="font-weight:700; font-size:13px;">
                            <td>${C.com_dt_ant || '03/09/2026'}</td><td>${fmtNum(C.com_l_ant || 1465)}</td>
                            <td>${C.com_dt_atu || '06/10/2026'}</td><td>${fmtNum(C.com_l_atu || 1627)}</td>
                            <td>${fmtNum(R.m3_caesb_com)} m³</td>
                            <td>${fmtBRL(C.com_fixa_agua || 30.51)}</td><td>${fmtBRL(C.com_fixa_esg || 30.51)}</td>
                            <td>${fmtBRL(C.com_var_agua || 3195.76)}</td><td>${fmtBRL(C.com_var_esg || 3195.76)}</td>
                            <td style="color:#fbbf24;">${fmtBRL(R.fatura_caesb_com)}</td>
                            <td style="color:#34d399;">${fmtNum(R.m3_comercial_5unid)} m³</td>
                            <td style="color:#fbbf24; font-size:14px;">${fmtBRL(R.preco_m3_com)} / m³</td>
                            <td style="color:#fbbf24;">${fmtBRL(R.arrecadacao_com_5unid)}</td>
                        </tr>
                    </tbody>
                </table>

                <table class="filterable-table">
                    <thead>
                        <tr>
                            <th>Estabelecimento</th><th>Unidade</th><th>Hidrômetro</th><th>Medidor</th>
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
        const lista = dadosModo.unidades_hidrometros || [];
        let rows = lista.map(r => `
            <tr>
                <td>${r.BLOCO || '-'}</td>
                <td>${r.ANDAR || '-'}</td>
                <td style="font-weight:700;">${r.UNIDADE}</td>
                <td>${badgeTipo(r.TIPO)}</td>
                <td><span class="hidro-badge">${r.HIDROMETRO || '-'}</span></td>
                <td><span class="med-badge">${r.MEDIDOR_VISUAL || '-'}</span></td>
                <td>${r.DATA_LEITURA || '-'}</td>
                <td>${r.TIPO === 'SEM_MEDIDOR' ? '-' : fmtNum(r.L_ANTERIOR)}</td>
                <td>${r.TIPO === 'SEM_MEDIDOR' ? '-' : fmtNum(r.VAL_ATU)}</td>
                <td style="font-weight:800; color:#34d399;">${r.TIPO === 'SEM_MEDIDOR' ? '-' : fmtNum(r.CONSUMO)}</td>
                <td>${r.FIXA_AGUA > 0 ? fmtBRL(r.FIXA_AGUA) : '-'}</td>
                <td>${r.FIXA_ESGOTO > 0 ? fmtBRL(r.FIXA_ESGOTO) : '-'}</td>
                <td>${r.V_AGUA > 0 ? fmtBRL(r.V_AGUA) : 'R$ 0,00'}</td>
                <td>${r.V_ESGOTO > 0 ? fmtBRL(r.V_ESGOTO) : '-'}</td>
                <td style="font-weight:700;">${r.SUBTOTAL > 0 ? fmtBRL(r.SUBTOTAL) : '-'}</td>
                <td>${r.VAL_RATEIO !== 0 ? fmtBRL(r.VAL_RATEIO) : '-'}</td>
                <td>${r.TAXA > 0 ? fmtBRL(r.TAXA) : '-'}</td>
                <td style="font-weight:800; color:#fbbf24;">${r.TOTAL !== 0 ? fmtBRL(r.TOTAL) : '-'}</td>
            </tr>
        `).join('');

        box.innerHTML = `
            <table id="dataTable" class="filterable-table">
                <thead>
                    <tr>
                        <th>Bloco</th><th>Andar</th><th>Unidade</th><th>Tipo</th>
                        <th>Hidrômetro</th><th>Medidor</th><th>Data Leit.</th>
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
                <td style="font-weight:700; color:#38bdf8;">${r.BLOCO}</td>
                <td>${r.ANDAR}</td>
                <td style="font-weight:800;">${r.UNIDADE_CURTA}</td>
                <td>${badgeTipo(r.TIPO)}</td>
                <td><span class="hidro-badge">${r.HIDROMETROS}</span></td>
                <td><span class="med-badge">${r.MEDIDORES}</span></td>
                <td>${r.QTD_MED}</td>
                <td>${(r.FRACAO * 100).toFixed(4)}%</td>
                <td style="font-weight:700; color:#34d399;">${r.TIPO === 'SEM_MEDIDOR' ? '-' : fmtNum(r.CONSUMO) + ' m³'}</td>
                <td style="font-weight:700;">${fmtBRL(r.SUBTOTAL)}</td>
                <td>${fmtBRL(r.VAL_RATEIO)}</td>
                <td>${fmtBRL(r.TAXA)}</td>
                <td style="font-weight:800; color:#fbbf24; font-size:14px;">${fmtBRL(r.TOTAL)}</td>
            </tr>
        `).join('');

        box.innerHTML = `
            <div class="exec-title green" style="margin-bottom:12px;">
                 LISTAGEM CONSOLIDADA POR UNIDADE (COM HIDRÔMETRO E MEDIDOR) — ADMINISTRADORA ÂNCORA (${dadosModo.cobranca_consolidada.length} UNIDADES)
            </div>
            <table id="dataTable" class="filterable-table">
                <thead>
                    <tr>
                        <th>Bloco</th><th>Andar</th><th>Unidade</th><th>Categoria</th>
                        <th>Hidrômetro(s)</th><th>Medidor(es)</th><th>Qtd</th>
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
                <td><span class="hidro-badge">${a.HIDROMETRO}</span></td>
                <td><span class="med-badge">${a.MEDIDOR}</span></td>
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
                <p>Para visualizar o confronto lado a lado, processe os dois arquivos: <b>2. Processar MIZAR</b> e <b>3. Processar WALK-BY</b>.</p>
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
                    <td>${uw.BLOCO}</td>
                    <td>${uw.ANDAR}</td>
                    <td style="font-weight:700;">${uw.UNIDADE_CURTA}</td>
                    <td><span class="hidro-badge">${uw.HIDROMETROS}</span></td>
                    <td><span class="med-badge">${uw.MEDIDORES}</span></td>
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

                <div class="exec-title green">COMPARATIVO UNIDADE POR UNIDADE COM HIDRÔMETRO E MEDIDOR (${estado.WALKBY.cobranca_consolidada.length} UNIDADES)</div>
                <table id="dataTable" class="filterable-table">
                    <thead>
                        <tr>
                            <th>Bloco</th><th>Andar</th><th>Unid.</th><th>Hidrômetro(s)</th><th>Medidor(es)</th>
                            <th>Mizar (m³)</th><th>Mizar Cons.</th><th>Mizar Rateio</th><th>MIZAR TOTAL</th>
                            <th>Walk-by (m³)</th><th>Walk-by Cons.</th><th>Walk-by Rateio</th><th>WALK-BY TOTAL</th>
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

            const wsGeral = XLSX.utils.json_to_sheet([D.resumo]);
            XLSX.utils.book_append_sheet(wb, wsGeral, `${pref} geral`);

            const wsUnds = XLSX.utils.json_to_sheet(D.unidades_hidrometros);
            XLSX.utils.book_append_sheet(wb, wsUnds, `${pref} unds`);

            const wsCob = XLSX.utils.json_to_sheet(D.cobranca_consolidada);
            XLSX.utils.book_append_sheet(wb, wsCob, `${pref} cobrança`);

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
        const C = R.caesb_detalhes || obterDadosCAESBFormulario();
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

        let y = 34;
        doc.setTextColor(0, 0, 0);
        doc.setFontSize(10);
        doc.setFont("helvetica", "bold");
        doc.text("1. DADOS DA FATURA CAESB RESIDENCIAL E FECHAMENTO DO RATEIO", 14, y);

        doc.autoTable({
            startY: y + 3,
            head: [['Leit. Ant. (' + (C.res_dt_ant || '03/09') + ')', 'Leit. Atual (' + (C.res_dt_atu || '06/10') + ')', 'Medido (m³)', 'Fixa Água/Esg.', 'Var. Água/Esg.', 'TOTAL CAESB']],
            body: [[
                fmtNum(C.res_l_ant || 411389), fmtNum(C.res_l_atu || 416637), fmtNum(R.m3_caesb_res) + ' m³',
                fmtBRL((C.res_fixa_agua || 8715) * 2), fmtBRL((C.res_var_agua || 22566.40) * 2), fmtBRL(R.fatura_caesb_res)
            ]],
            theme: 'grid',
            headStyles: { fillColor: [16, 185, 129], halign: 'center' },
            bodyStyles: { halign: 'center', fontStyle: 'bold' }
        });

        y = doc.lastAutoTable.finalY + 5;
        doc.autoTable({
            startY: y,
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

        y = doc.lastAutoTable.finalY + 9;
        doc.text("2. DEMONSTRATIVO DO SETOR COMERCIAL (COM HIDRÔMETRO E MEDIDOR)", 14, y);

        const comRows = D.unidades_hidrometros.filter(x => x.TIPO === 'COMERCIAL' || x.TIPO === 'COMERCIAL_OMO');
        doc.autoTable({
            startY: y + 3,
            head: [['Unidade Comercial', 'Hidrômetro', 'Medidor', 'L. Ant.', 'L. Atual', 'Consumo', 'Valor Água', 'Tx. Leit.', 'Total (R$)']],
            body: comRows.map(c => [
                c.UNIDADE_RAW, c.HIDROMETRO, c.MEDIDOR_VISUAL, fmtNum(c.L_ANTERIOR), fmtNum(c.VAL_ATU),
                fmtNum(c.CONSUMO) + ' m³', fmtBRL(c.V_AGUA), fmtBRL(c.TAXA), fmtBRL(c.V_AGUA + c.TAXA)
            ]),
            theme: 'striped',
            headStyles: { fillColor: [217, 119, 6] },
            columnStyles: { 6: { halign: 'right' }, 7: { halign: 'right' }, 8: { halign: 'right', fontStyle: 'bold' } }
        });

        y = doc.lastAutoTable.finalY + 9;
        doc.text("3. AUDITORIA DE ALARMES E ANOMALIAS TÉCNICAS", 14, y);
        const A = R.contagem_alarmes;
        doc.autoTable({
            startY: y + 3,
            head: [['Medidor Bloqueado', 'Vazamento', 'Sub-vazão', 'Fluxo Reverso', 'Desmontagem', 'Sem Transmissão']],
            body: [[A.bloqueado, A.vazamento, A.subvazao, A.retorno, A.desmontagem, A.sem_transmissao]],
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
    preview = MotorFaturamento.obter_preview_base_fixa()
    out = {
        'qtd_base_fixa': len(preview),
        'base_fixa_preview': preview
    }
    for ch in ['MIZAR', 'WALKBY']:
        reg = EstadoCalculoV4.query.filter_by(chave=ch).first()
        if reg and reg.dados_json:
            try: out[ch] = json.loads(reg.dados_json)
            except: pass
    return jsonify(out)

@app.route('/parse_conta_caesb', methods=['POST'])
def parse_conta_caesb():
    try:
        dados = request.get_json(force=True) or {}
        texto = dados.get('texto', '')
        resultado = MotorFaturamento.extrair_dados_conta_caesb_texto(texto)
        return jsonify(resultado)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/upload_base', methods=['POST'])
def upload_base():
    if 'file' not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado."}), 400
    f = request.files['file']
    try:
        info = MotorFaturamento.ler_planilha_ou_txt(f.read(), f.filename)
        adicionados = MotorFaturamento.importar_ou_atualizar_base_fixa(info)
        preview = MotorFaturamento.obter_preview_base_fixa()
        return jsonify({
            "qtd": adicionados,
            "base_fixa_preview": preview,
            "message": f"Base Fixa Permanente gravada em 0,2s! {adicionados} registros (com Hidrômetro, Medidor, Unidade 251 e Frações) travados no banco."
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/virar_mes', methods=['POST'])
def virar_mes():
    try:
        dados = request.get_json(force=True) or {}
        modo = dados.get('modo', 'WALKBY')
        reg = EstadoCalculoV4.query.filter_by(chave=modo).first()
        if not reg or not reg.dados_json:
            return jsonify({"error": "Nenhum fechamento processado para virar o mês."}), 400

        fechamento = json.loads(reg.dados_json)
        mapa_atu = {str(x['MEDIDOR_DNA']): float(x['VAL_ATU']) for x in fechamento.get('unidades_hidrometros', [])}

        atualizados = 0
        with db.session.no_autoflush:
            for m in MedidorBaseV4.query.all():
                if m.medidor_dna in mapa_atu and m.tipo != 'SEM_MEDIDOR':
                    novo_val = mapa_atu[m.medidor_dna]
                    if novo_val >= 1000000:
                        novo_val = novo_val - 1000000
                    m.l_anterior = novo_val
                    atualizados += 1
        db.session.commit()
        return jsonify({"message": f"Ciclo virado com sucesso! {atualizados} leituras atuais foram salvas como Leitura Anterior para o próximo mês."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/processar', methods=['POST'])
def processar():
    if 'file' not in request.files:
        return jsonify({"error": "Arquivo de leitura não enviado."}), 400
    f = request.files['file']
    modo = request.form.get('modo', 'WALKBY').upper()
    try:
        dados_caesb = json.loads(request.form.get('dados_caesb', '{}'))
    except:
        dados_caesb = {}

    try:
        info = MotorFaturamento.ler_planilha_ou_txt(f.read(), f.filename)
        resultado = MotorFaturamento.executar_fechamento(
            info_arq=info, modo_leitura=modo, dados_caesb=dados_caesb
        )

        reg = EstadoCalculoV4.query.filter_by(chave=modo).first()
        if not reg:
            reg = EstadoCalculoV4(chave=modo, dados_json=json.dumps(resultado))
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
