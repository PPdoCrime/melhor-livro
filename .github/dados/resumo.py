#!/usr/bin/env python3
"""Lê os eventos do livro no PostHog e grava só números agregados.

Roda nos servidores do GitHub (a chave fica no segredo POSTHOG_KEY).
Nunca grava: IP, cidade, id de leitor, texto digitado, horário de um leitor específico.
Nunca imprime dados no log (o log de um repositório público é público).
Uso: python3 resumo.py <pasta_de_saida>
"""
import collections
import datetime
import json
import os
import statistics
import sys
import urllib.error
import urllib.request

HOST = os.environ.get('POSTHOG_HOST_TESTE', 'https://us.posthog.com')  # variável só para teste local
PHC = 'phc_n6BdZjg46GCSRYpP66xjQ79Uicz7TGbNiT4TqxAybzij'  # chave pública do projeto (já está no site)
DIAS = 180
CAPS = [str(i) for i in range(17)] + ['fim']
ORD = {c: i for i, c in enumerate(CAPS)}
NOME = {c: ('prólogo' if c == '0' else 'fim' if c == 'fim' else 'cap ' + c) for c in CAPS}
BRT = datetime.timezone(datetime.timedelta(hours=-3))

SAIDA = sys.argv[1] if len(sys.argv) > 1 else 'saida'
os.makedirs(SAIDA, exist_ok=True)
KEY = os.environ.get('POSTHOG_KEY', '').strip()


def limpa(s):
    s = str(s)
    return s.replace(KEY, '***') if KEY else s


def status(ok, **kw):
    kw.update(ok=ok, quando=datetime.datetime.now(BRT).strftime('%Y-%m-%d %H:%M (Brasília)'))
    with open(os.path.join(SAIDA, 'status.json'), 'w') as f:
        json.dump(kw, f, ensure_ascii=False, indent=1)
    print('ok' if ok else 'erro (detalhes em status.json no ramo dados)')


def _falha(tipo, valor, tb):
    # erro inesperado: registra só o tipo e a linha (a mensagem poderia conter dados)
    import traceback
    linhas = [f.lineno for f in traceback.extract_tb(tb)]
    status(False, erro=f'{tipo.__name__} na linha {linhas[-1] if linhas else "?"} do resumo.py')


sys.excepthook = _falha


def chamar(caminho, corpo=None):
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(HOST + caminho, data=dados, method='POST' if dados else 'GET',
                                 headers={'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        corpo_erro = e.read()[:400].decode('utf-8', 'replace')
        return None, limpa(f'{caminho} -> HTTP {e.code}: {corpo_erro}')
    except Exception as e:  # noqa: BLE001
        return None, limpa(f'{caminho} -> {e!r}')[:400]


# ---------- 1. achar o projeto ----------
if not KEY:
    status(False, erro='o segredo POSTHOG_KEY está vazio ou não existe')
    sys.exit(0)

erros = []
alvos = []
r, e = chamar('/api/projects/')
if r:
    proj = r.get('results', [])
    certo = [str(p['id']) for p in proj if p.get('api_token') == PHC]
    alvos += certo or ([str(proj[0]['id'])] if len(proj) == 1 else [])
else:
    erros.append(e)
alvos.append('@current')

SQL = (f"SELECT event, timestamp, properties FROM events "
       f"WHERE timestamp > now() - INTERVAL {DIAS} DAY AND event NOT LIKE '$%' "
       f"ORDER BY timestamp LIMIT 50000")
res = None
for alvo in dict.fromkeys(alvos):
    for tipo in ('projects', 'environments'):
        r, e = chamar(f'/api/{tipo}/{alvo}/query/', {'query': {'kind': 'HogQLQuery', 'query': SQL}})
        if r is not None and 'results' in r:
            res = r
            break
        erros.append(e)
    if res:
        break
if res is None:
    status(False, erro='não consegui consultar o PostHog', tentativas=erros[-6:])
    sys.exit(0)

# ---------- 2. organizar ----------
evs = []
for ev, ts, props in res['results']:
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except ValueError:
            props = {}
    try:
        t = datetime.datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
        if t.tzinfo is None:
            t = t.replace(tzinfo=datetime.timezone.utc)
        t = t.astimezone(BRT)
    except ValueError:
        t = None
    evs.append((ev, t, props or {}))


def eh_teste(p):
    return p.get('teste') in (True, 'true', 1, '1')


# aparelho marcado como teste em algum momento = todos os eventos dele são do autor
leitores_teste = {p.get('leitor') for _, _, p in evs if eh_teste(p)}
testes = [x for x in evs if x[2].get('leitor') in leitores_teste or eh_teste(x[2])]
reais = [x for x in evs if not (x[2].get('leitor') in leitores_teste or eh_teste(x[2]))]


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def mediana(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def quartis(xs):
    xs = sorted(x for x in xs if x is not None)
    if len(xs) < 4:
        return None
    q = statistics.quantiles(xs, n=4)
    return [round(q[0], 1), round(q[2], 1)]


L = collections.defaultdict(lambda: {'caps': set(), 'concl': set(), 'seg': collections.Counter(), 'versao': set(),
                                      'convite': None, 'dias': set(), 'aberturas': 0, 'aparelho': {}})
palavras = {}
for ev, t, p in reais:
    lid = p.get('leitor')
    if not lid:
        continue
    d = L[lid]
    if p.get('versao'):
        d['versao'].add(p['versao'])
    if p.get('convite') and not d['convite']:
        d['convite'] = str(p['convite'])
    if t:
        d['dias'].add(t.date().isoformat())
    cap = str(p.get('cap')) if p.get('cap') is not None else None
    if ev == 'livro_aberto':
        d['aberturas'] += 1
        if not d['aparelho']:
            d['aparelho'] = {k: p.get(k) for k in ('largura', 'altura', 'toque', 'tema')}
    elif ev == 'capitulo_chegou' and cap in ORD:
        d['caps'].add(cap)
    elif ev == 'capitulo_concluido' and cap in ORD:
        d['concl'].add(cap)
    elif ev == 'capitulo_tempo' and cap in ORD:
        d['caps'].add(cap)
        d['seg'][cap] += num(p.get('segundos')) or 0
        if num(p.get('palavras')):
            palavras[cap] = max(palavras.get(cap, 0), int(num(p['palavras'])))


def ultimo(d):
    return max(d['caps'], key=ORD.get) if d['caps'] else None


todos = [d for d in L.values() if d['aberturas'] or d['caps'] or d['concl']]


def perfil(d):
    # robô provável: "concluiu" capítulos em que nunca chegou rolando (página renderizada inteira de uma vez,
    # típico de verificador de links, antivírus ou gerador de prévia)
    if len(d['concl'] - d['caps']) >= 3:
        return 'robo_provavel'
    if not d['caps']:
        return 'so_abriu'
    total = sum(d['seg'].values())
    if any(lida(d, c) for c in d['caps']):
        return 'leu_algum_trecho'
    return 'folheou' if total >= 3 else 'so_rolou'


def lida(d, c):
    # trecho lido de verdade: ao menos 40% do tempo esperado a 180 palavras/min (ou seja, até ~450 ppm)
    pal = palavras.get(c)
    return bool(pal) and d['seg'][c] >= 0.4 * pal / 180 * 60


def faixa(v, cortes, rot):
    v = num(v)
    if v is None:
        return '?'
    for c, r in zip(cortes, rot):
        if v <= c:
            return r
    return rot[-1]


perfis = {}
for d in todos:
    d['perfil'] = perfil(d)
for nome_p in ('robo_provavel', 'so_abriu', 'so_rolou', 'folheou', 'leu_algum_trecho'):
    ds = [d for d in todos if d['perfil'] == nome_p]
    if not ds:
        continue
    ap = [d['aparelho'] for d in ds]
    perfis[nome_p] = {
        'n': len(ds),
        'largura': dict(collections.Counter(faixa(a.get('largura'), [360, 430, 767], ['até 360', '361-430', '431-767', '768+']) for a in ap)),
        'altura': dict(collections.Counter(faixa(a.get('altura'), [700, 950, 1400], ['até 700', '701-950', '951-1400', '1401+']) for a in ap)),
        'toque': dict(collections.Counter(str(a.get('toque')) for a in ap)),
        'dias': dict(collections.Counter(min(d['dias']) if d['dias'] else '?' for d in ds)),
        'convite': dict(collections.Counter(d['convite'] or '(sem)' for d in ds)),
    }

leitores = [d for d in todos if d['perfil'] != 'robo_provavel']
robos = {lid for lid, d in L.items() if d.get('perfil') == 'robo_provavel'}
reais = [x for x in reais if x[2].get('leitor') not in robos]  # o resto do resumo ignora os robôs
n_leit = len(leitores)
leram = {NOME[c]: sum(1 for d in leitores if c in d['caps'] and lida(d, c)) for c in CAPS}

# comparação entre versões (ex.: v6 mostrava "16 capítulos" logo de cara; v7 não mostra)
por_versao = {}
for d in leitores:
    v = sorted(d['versao'])[0] if d['versao'] else '?'
    x = por_versao.setdefault(v, {'leitores': 0, 'perfis': collections.Counter(), 'chegaram_prologo': 0,
                                  'leram_prologo': 0, 'chegaram_cap1': 0, 'leram_cap1': 0})
    x['leitores'] += 1
    x['perfis'][d['perfil']] += 1
    x['chegaram_prologo'] += '0' in d['caps']
    x['leram_prologo'] += '0' in d['caps'] and lida(d, '0')
    x['chegaram_cap1'] += '1' in d['caps']
    x['leram_cap1'] += '1' in d['caps'] and lida(d, '1')
for x in por_versao.values():
    x['perfis'] = dict(x['perfis'])

alcance = {NOME[c]: sum(1 for d in leitores if c in d['caps']) for c in CAPS}
concluiu = {NOME[c]: sum(1 for d in leitores if c in d['concl']) for c in CAPS}
param = collections.Counter(NOME[ultimo(d)] if ultimo(d) else 'só abriu' for d in leitores)
param = {k: param[k] for k in ['só abriu'] + [NOME[c] for c in CAPS] if param.get(k)}

tempo = {}
for c in CAPS:
    segs = [d['seg'][c] for d in leitores if d['seg'][c] >= 3]
    if not segs:
        continue
    pal = palavras.get(c)
    ppms = [round(pal / (s / 60)) for s in segs] if pal else []
    tempo[NOME[c]] = {
        'leitores': len(segs),
        'min_mediana': round(mediana(segs) / 60, 1),
        'min_quartis': [round(x / 60, 1) for x in quartis(segs)] if quartis(segs) else None,
        'min_esperado_180ppm': round(pal / 180, 1) if pal else None,
        'ppm_mediana': mediana(ppms),
        'passou_batido_ppm_acima_600': sum(1 for x in ppms if x > 600),
    }

# ---------- 3. contagens gerais ----------
por_ev = collections.Counter(ev for ev, _, _ in reais)
por_ev_teste = collections.Counter(ev for ev, _, _ in testes)


def conta(evento, chave, fn=lambda v: v):
    c = collections.Counter()
    for ev, _, p in reais:
        if ev == evento:
            c[str(fn(p.get(chave)))] += 1
    return dict(c.most_common(15))


def largura(v):
    v = num(v)
    if v is None:
        return '?'
    return 'até 360' if v <= 360 else '361-430' if v <= 430 else '431-767' if v <= 767 else '768+'


abertura = {k: conta('livro_aberto', k) for k in ('app', 'tema', 'toque', 'origem', 'entrou_por', 'retorno', 'versao')}
abertura['largura'] = conta('livro_aberto', 'largura', largura)

conv = collections.defaultdict(list)
for d in leitores:
    conv[d['convite'] or '(sem convite)'].append(d)
convites = {}
for k, ds in conv.items():
    convites[k] = {
        'leitores': len(ds),
        'chegaram_cap1': sum(1 for d in ds if '1' in d['caps']),
        'chegaram_cap4': sum(1 for d in ds if '4' in d['caps']),
        'chegaram_cap8': sum(1 for d in ds if '8' in d['caps']),
        'chegaram_fim': sum(1 for d in ds if 'fim' in d['caps']),
        'min_lidos_mediana': round(mediana([sum(d['seg'].values()) / 60 for d in ds]) or 0, 1),
    }

mov_seg = collections.Counter()
for ev, _, p in reais:
    if ev == 'capitulo_tempo':
        mov_seg[str(p.get('movimento'))] += num(p.get('segundos')) or 0
movimento = {k: round(v / 60, 1) for k, v in mov_seg.most_common()}

saidas = collections.Counter()
motivos = collections.defaultdict(collections.Counter)
for ev, _, p in reais:
    cap = str(p.get('cap'))
    if ev == 'saiu_do_app' and cap in ORD:
        saidas[NOME[cap]] += 1
    if ev == 'capitulo_tempo' and cap in ORD:
        motivos[NOME[cap]][str(p.get('motivo'))] += 1

sess = [p for ev, _, p in reais if ev == 'sessao_fim']
sessoes = {
    'n': len(sess),
    'minutos_mediana': mediana([num(p.get('minutos')) for p in sess]),
    'cap_final': dict(collections.Counter(NOME.get(str(p.get('cap_final')), str(p.get('cap_final'))) for p in sess).most_common()),
}

confira = {}
for ev, _, p in reais:
    if ev != 'confira':
        continue
    c = NOME.get(str(p.get('cap')), str(p.get('cap')))
    x = confira.setdefault(c, {'primeira_tentativa': 0, 'acertou_de_primeira': 0, 'toques': 0})
    x['toques'] += 1
    if p.get('primeira_tentativa') in (True, 'true'):
        x['primeira_tentativa'] += 1
        x['acertou_de_primeira'] += p.get('certo') in (True, 'true')
confira = {k: confira[k] for k in sorted(confira, key=lambda k: ORD.get(k.replace('cap ', '').replace('prólogo', '0'), 99))}

interacoes = {
    'aposta_prologo': conta('aposta_prologo', 'valor'),
    'aposta_widget': {f"{NOME.get(str(p.get('cap')), p.get('cap'))}: {p.get('valor')}": 0 for ev, _, p in reais if ev == 'aposta'},
    'contexto': conta('contexto', 'valor'),
    'nana_notou': conta('nana', 'notou'),
    'pensar_escreveu': conta('pensar', 'escreveu'),
    'maquina_aba': conta('maquina_aba', 'modo'),
    'maquina_regra': conta('maquina_regra', 'regra'),
    'whatsapp_pergunta': conta('whatsapp', 'pergunta'),
    'compartilhou': conta('compartilhou', 'tipo'),
    'mapa_pulou_para': conta('mapa_pulou', 'para'),
}
for ev, _, p in reais:
    if ev == 'aposta':
        k = f"{NOME.get(str(p.get('cap')), p.get('cap'))}: {p.get('valor')}"
        interacoes['aposta_widget'][k] += 1
passaro = [p for ev, _, p in reais if ev == 'passaro']
interacoes['passaro'] = {'n': len(passaro), 'acertos_mediana': mediana([num(p.get('acertos')) for p in passaro])}

dias = collections.defaultdict(lambda: {'leitores': set(), 'eventos': 0})
horas = collections.Counter()
for ev, t, p in reais:
    if not t:
        continue
    dd = dias[t.date().isoformat()]
    dd['leitores'].add(p.get('leitor'))
    dd['eventos'] += 1
    if ev == 'capitulo_tempo':
        horas[t.hour] += (num(p.get('segundos')) or 0) / 60
por_dia = {k: {'leitores': len(v['leitores']), 'eventos': v['eventos']} for k, v in sorted(dias.items())}
min_por_hora = {f'{h:02d}h': round(horas[h], 1) for h in range(24) if horas[h]}

erros_js = conta('erro_js', 'msg')

# ---------- atividade recente (para saber se alguém está lendo agora) ----------
agora = datetime.datetime.now(BRT)


def janela(horas):
    corte = agora - datetime.timedelta(hours=horas)
    rec = [(ev, t, p) for ev, t, p in reais if t and t >= corte]
    seg = collections.defaultdict(collections.Counter)
    for ev, t, p in rec:
        if ev == 'capitulo_tempo' and str(p.get('cap')) in ORD:
            seg[p.get('leitor')][str(p.get('cap'))] += num(p.get('segundos')) or 0
    lidos = collections.Counter()
    for lid, cs in seg.items():
        for c, sg in cs.items():
            if palavras.get(c) and sg >= 0.4 * palavras[c] / 180 * 60:
                lidos[NOME[c]] += 1
    ult = max((t for _, t, _ in rec), default=None)
    return {
        'leitores_ativos': len({p.get('leitor') for _, _, p in rec}),
        'aberturas': sum(1 for ev, _, _ in rec if ev == 'livro_aberto'),
        'chegaram_em': dict(collections.Counter(NOME[str(p.get('cap'))] for ev, _, p in rec
                                                if ev == 'capitulo_chegou' and str(p.get('cap')) in ORD)),
        'leram_de_verdade': dict(lidos),
        'minutos_lidos': round(sum(sum(cs.values()) for cs in seg.values()) / 60, 1),
        'ultimo_evento': ult.strftime('%d/%m %H:%M') if ult else None,
    }


recente = {'ultimas_3h': janela(3), 'ultimas_24h': janela(24)}

resumo = {
    'atualizado': datetime.datetime.now(BRT).strftime('%Y-%m-%d %H:%M (Brasília)'),
    'janela_dias': DIAS,
    'eventos_lidos': len(evs),
    'limite_atingido': bool(res.get('hasMore')) or len(evs) >= 50000,
    'leitores': n_leit,
    'recente': recente,
    'leitores_que_voltaram_outro_dia': sum(1 for d in leitores if len(d['dias']) > 1),
    'versoes': dict(collections.Counter(','.join(sorted(d['versao'])) or '?' for d in leitores)),
    'robos_provaveis_excluidos': perfis.get('robo_provavel', {}).get('n', 0),
    'perfis': perfis,
    'leram_de_verdade_por_capitulo': leram,
    'por_versao': por_versao,
    'alcance_leitores_por_capitulo': alcance,
    'concluiram_por_capitulo': concluiu,
    'onde_pararam_ultimo_capitulo': param,
    'tempo_por_capitulo': tempo,
    'convites': convites,
    'abertura': abertura,
    'movimento_minutos': movimento,
    'saiu_do_app_por_capitulo': dict(saidas),
    'motivo_fim_de_trecho_por_capitulo': {k: dict(v) for k, v in motivos.items()},
    'sessoes': sessoes,
    'confira': confira,
    'interacoes': interacoes,
    'por_dia': por_dia,
    'minutos_lidos_por_hora_do_dia': min_por_hora,
    'erros_js': erros_js,
    'eventos_por_tipo': dict(por_ev.most_common()),
    'eventos_do_autor_teste': dict(por_ev_teste.most_common()),
}
with open(os.path.join(SAIDA, 'resumo.json'), 'w') as f:
    json.dump(resumo, f, ensure_ascii=False, indent=1)

# ---------- 4. versão para ler no celular ----------
m = [f"# Leitura do livro — {resumo['atualizado']}", '',
     f"**{n_leit} leitores** (fora você e {resumo['robos_provaveis_excluidos']} robôs prováveis). "
     f"{resumo['leitores_que_voltaram_outro_dia']} voltaram em outro dia.", '',
     'Perfis: ' + ', '.join(f"{k} {v['n']}" for k, v in perfis.items()), '',
     '## Agora', ''] + [f"- {k.replace('_', ' ')}: {v['leitores_ativos']} leitores, {v['minutos_lidos']} min lidos, "
                        f"chegaram em {v['chegaram_em'] or '–'}, leram de verdade {v['leram_de_verdade'] or '–'}, "
                        f"último evento {v['ultimo_evento'] or '–'}" for k, v in recente.items()] + ['',
     '## Até onde chegaram', '', '| trecho | chegaram | leram de verdade | tempo típico (min) | esperado (min) |',
     '|---|---|---|---|---|']
for c in CAPS:
    nm = NOME[c]
    tp = tempo.get(nm, {})
    if alcance[nm] or tp:
        m.append(f"| {nm} | {alcance[nm]} | {leram[nm]} | {tp.get('min_mediana', '–')} | {tp.get('min_esperado_180ppm', '–')} |")
m += ['', '## Por versão', ''] + [f"- {k}: {v['leitores']} leitores, {v['perfis'].get('so_abriu', 0)} só abriram, "
                                   f"{v['leram_prologo']} leram o prólogo, {v['leram_cap1']} leram o cap 1"
                                   for k, v in sorted(por_versao.items())]
m += ['', '## Onde pararam', ''] + [f'- {k}: {v}' for k, v in param.items()]
m += ['', '## Convites', ''] + [f"- {k}: {v['leitores']} leitores, {v['chegaram_cap1']} no cap 1, {v['chegaram_cap8']} no cap 8, {v['chegaram_fim']} no fim"
                                for k, v in sorted(convites.items(), key=lambda kv: -kv[1]['leitores'])]
m += ['', '## Confira (acerto de primeira)', ''] + [f"- {k}: {v['acertou_de_primeira']}/{v['primeira_tentativa']}" for k, v in confira.items()]
m += ['', f"Eventos seus (teste) no período: {sum(por_ev_teste.values())}. Detalhes em resumo.json."]
with open(os.path.join(SAIDA, 'resumo.md'), 'w') as f:
    f.write('\n'.join(m) + '\n')

status(True, eventos=len(evs), leitores=n_leit, avisos=erros[-3:] if erros else [])
