# Refatoração Multi-Torneio — Sampa Volleyball League

## Contexto

Hoje o sistema organiza tudo por **naipe** (`masculino`/`feminino`). Cada naipe tem UMA competição: suas equipes, grupos, jogos, classificação e configuração.

**Problema:** agora existem **múltiplos torneios independentes por categoria** rodando em datas próximas (ex: torneio "E1" no dia 26/07 e "E2" no dia 25/07, ambos femininos, categorias diferentes). O modelo atual não suporta isso:
- Não dá pra saber em qual torneio uma equipe se inscreveu
- Não dá pra ter tabelas/classificações separadas e simultâneas
- Quem se inscreveu no torneio do dia 26 "não enxerga nada" enquanto o do dia 25 está rodando

## Objetivo

Transformar a unidade organizadora de **naipe** para **torneio**. Cada torneio é uma competição completa e independente, com suas próprias equipes, grupos, jogos, classificação e configuração de formato. Um torneio pertence a um naipe e tem uma categoria.

## Requisitos de negócio (confirmados com o cliente)

1. **Uma equipe pode se inscrever em vários torneios**, cada inscrição com sua própria taxa/pagamento. Modelo escolhido: **1 inscrição = 1 equipe + 1 torneio**. Se a equipe QUEEN joga E1 e E2, ela aparece como **2 inscrições separadas** (2 linhas na lista), cada uma com status de pagamento próprio.

2. **E1 e E2 são categorias diferentes** (não é o mesmo torneio em dias diferentes — são competições distintas).

3. **Na inscrição**, o torneio vem **pré-selecionado pela URL** do card que a equipe clicou, mas a equipe **pode trocar** num dropdown no formulário.

4. **Admin pode mover** uma equipe de um torneio para outro.

5. **Torneios podem rodar simultaneamente** — cada um com sua tela viva (tabela, classificação, ao vivo) ao mesmo tempo. Este é o ponto crítico que a solução precisa resolver: quem se inscreveu no torneio do dia 26 precisa ver a tela do SEU torneio a qualquer momento.

## Estrutura de dados atual (app.py — DEFAULT_DATA)

```python
DEFAULT_DATA = {
    "etapas": {"masculino": [], "feminino": []},
    "equipes": {"masculino": [], "feminino": []},
    "atletas": {},  # chaveado por equipe_id
    "config": {
        "masculino": {"max_equipes": 8, "formato_jogos": "grupos", "hora_inicio": "08:30", "intervalo_min": 75},
        "feminino": {"max_equipes": 6, "formato_jogos": "hexagonal", "hora_inicio": "08:30", "intervalo_min": 75}
    },
    "grupos": {"masculino": {"A": [], "B": []}, "feminino": {"A": [], "B": []}},
    "jogos": {"masculino": [], "feminino": []},
    "regulamento": {"masculino": "", "feminino": ""},
    "settings": { ... },  # global, não muda
    "patrocinadores": [],  # global, não muda
    "admin_password_hash": ""
}
```

Hoje "etapas" já é uma lista por naipe — cada item de etapa tem: `{id, nome, local, endereco, data, categoria, formato, horario}`. **A ideia é que cada "etapa" VIRE um "torneio"** e passe a ser o container de equipes/grupos/jogos/config.

## Estrutura de dados proposta

Migrar de "chaveado por naipe" para "chaveado por torneio_id". Cada torneio carrega seu naipe e categoria.

```python
DEFAULT_DATA = {
    # Lista de torneios (era "etapas"). Cada torneio é uma competição completa.
    "torneios": [
        # {
        #   "id": "abc123",
        #   "naipe": "feminino",         # masculino | feminino
        #   "nome": "Detox da Copa",
        #   "categoria": "E2",           # E1, E2, 30+, etc — categoria/nível
        #   "local": "S.P.F.C.",
        #   "endereco": "...",
        #   "data": "2026-07-25",
        #   "horario": "08:30",
        #   "formato_jogos": "quad_decisao",  # hexagonal|grupos|quad_corrido|quad_decisao|tri_corrido|tri_final
        #   "max_equipes": 4,
        #   "hora_inicio": "08:30",
        #   "intervalo_min": 75,
        #   "taxa": 0,                   # opcional: valor da taxa de inscrição
        #   "adiado": False,             # substitui a checagem hard-coded de data '2026-05-09'
        #   "regulamento": "",           # regulamento por torneio (ou manter global — decidir)
        #   "created_at": "..."
        # }
    ],

    # Equipes agora carregam torneio_id. Uma equipe em 2 torneios = 2 objetos.
    "equipes": [
        # {
        #   "id": "...",
        #   "torneio_id": "abc123",      # NOVO: a qual torneio esta inscrição pertence (pode ser null p/ legado)
        #   "nome": "QUEEN",
        #   "responsavel": "Zuleide e Neide",
        #   "telefone": "...",
        #   "login": "queen",
        #   "senha_hash": "...",
        #   "pagamento_status": "pendente",  # por inscrição = por torneio
        #   "comprovante": null,
        #   "logo": null,
        #   "foto": null,
        #   "is_test": false,
        #   "created_at": "..."
        # }
    ],

    "atletas": {},  # chaveado por equipe_id (não muda — equipe_id já é único por inscrição)

    # Grupos, jogos: agora chaveados por torneio_id em vez de naipe
    "grupos": {
        # "abc123": {"A": [equipe_id, ...], "B": [...]}
    },
    "jogos": {
        # "abc123": [ {jogo...}, ... ]
    },

    # Settings e patrocinadores continuam GLOBAIS (não mudam)
    "settings": { ... },
    "patrocinadores": [],
    "admin_password_hash": ""
}
```

**Decisão de design:** `equipes` deixa de ser `{"masculino": [], "feminino": []}` e vira uma **lista única** onde cada item tem `torneio_id`. O naipe da equipe é derivado do torneio (`torneio.naipe`). Isso evita duplicar a lógica de naipe.

## Endpoints da API — mudanças

Trocar o parâmetro `<naipe>` por `<torneio_id>` na maioria das rotas:

| Antes | Depois |
|-------|--------|
| `GET /api/etapas/<naipe>` | `GET /api/torneios` (lista todos) e `GET /api/torneios/<naipe>` (filtra por naipe) |
| `POST /api/etapas/<naipe>` | `POST /api/torneios` (cria, recebe naipe no body) |
| `PUT /api/etapas/<naipe>/<id>` | `PUT /api/torneios/<id>` |
| `DELETE /api/etapas/<naipe>/<id>` | `DELETE /api/torneios/<id>` |
| `GET /api/equipes/<naipe>` | `GET /api/equipes?torneio_id=<id>` (público, filtra) |
| `GET /api/equipes/<naipe>/admin` | `GET /api/equipes/admin?torneio_id=<id>` |
| `POST /api/equipes/<naipe>` | `POST /api/equipes` (torneio_id no body) |
| `GET /api/jogos/<naipe>` | `GET /api/jogos/<torneio_id>` |
| `POST /api/jogos/<naipe>/gerar` | `POST /api/jogos/<torneio_id>/gerar` |
| `GET /api/classificacao/<naipe>/<grupo>` | `GET /api/classificacao/<torneio_id>/<grupo>` |
| `GET/POST /api/grupos/<naipe>` | `GET/POST /api/grupos/<torneio_id>` |
| `GET/POST /api/config/<naipe>` | Config vira propriedade do torneio: `PUT /api/torneios/<id>` atualiza formato/hora/intervalo |
| `POST /api/jogos/<naipe>/atualizar-horarios` | `POST /api/jogos/<torneio_id>/atualizar-horarios` |
| Ao vivo, iniciar, pontos, encerrar, parciais | trocar `<naipe>` por `<torneio_id>` |

**Novo endpoint:** `POST /api/equipes/<equipe_id>/mover` — move inscrição de um torneio pra outro (body: `{torneio_id_novo}`).

## Formatos de jogo (já implementados — manter)

O sistema já suporta: `hexagonal`, `grupos`, `quad_corrido`, `quad_decisao`. Há um pedido pendente de adicionar `tri_corrido` e `tri_final` (triangular, 3 equipes) e **melhor de 5 sets** como opção de configuração — ver seção "Pendências" abaixo. A config de formato/hora/intervalo passa a ser **por torneio** em vez de por naipe.

## Frontend — mudanças principais

### Landing (landing.html)
- O card da home já lista os próximos torneios (feito). Cada botão "Inscreva-se" deve incluir o torneio na URL: `/app#sec-inscricao?torneio=<id>`.

### App (index.html)
- **Seletor de torneio:** em vez de (ou além de) alternar naipe masculino/feminino, o usuário escolhe **qual torneio** está vendo. Isso afeta todas as telas (jogos, classificação, equipes, ao vivo).
- **Formulário de inscrição:** adicionar dropdown "Torneio", pré-selecionado pela query string `?torneio=<id>` da URL, mas editável.
- **Lista de equipes (admin):** filtro por torneio; mostrar a qual torneio cada equipe pertence; botão "mover para outro torneio"; equipes com `torneio_id: null` aparecem num grupo "⚠ Sem torneio definido" para o admin atribuir.
- **Lista WhatsApp:** separar/filtrar por torneio.
- **Modo Teste:** o ambiente de teste isolado deve criar um torneio de teste também (hoje ele cria equipes/jogos com `is_test`; adaptar para `torneio_id` de teste). Manter todo o isolamento (invisível ao público).
- **Contador max_equipes:** por torneio, não por naipe.

## Migração de dados existentes

Ao subir a versão nova, rodar uma migração única:
1. Para cada item em `etapas[naipe]`, criar um `torneio` correspondente (herda naipe, copia config do naipe como formato/hora/intervalo default).
2. Para cada equipe em `equipes[naipe]`, setar `torneio_id = null` (legado — admin atribui manualmente depois). OU, se houver só 1 torneio no naipe, vincular automaticamente.
3. Migrar `grupos[naipe]` e `jogos[naipe]` para o torneio correspondente (se houver só 1 torneio por naipe no momento da migração).
4. **Importante:** a equipe QUEEN (e outras já inscritas) ficará com `torneio_id: null`. O admin precisa perguntar a elas qual torneio querem e atribuir manualmente. Não há como recuperar esse dado — ele nunca foi capturado.

## Pendências separadas (implementar junto ou depois)

Estas são solicitações que estavam em aberto antes da refatoração:

1. **Triangular:** adicionar formatos `tri_corrido` (3 equipes, 3 jogos, pontos corridos) e `tri_final` (3 jogos + final 1ºx2º). Lógica análoga a `quad_corrido`/`quad_decisao`. NOTA: cliente pediu triangular COM final apesar de saber que é menos justo (3º joga 1 jogo a menos).

2. **Melhor de 5 sets:** hoje todos os jogos são melhor de 3 (encerra em 2 sets). Cliente quer que triangular use **melhor de 5**. Melhor implementar como **config por torneio** (`melhor_de: 3 | 5`) que afeta a lógica do Modo Ao Vivo (quantos sets pra encerrar) e a auto-classificação. Isso mexe no fluxo de encerramento de partida ao vivo.

3. **SEO / indexação Google:** o site não aparece nas buscas. Falta: `robots.txt`, `sitemap.xml`, e submissão no Google Search Console (esta parte é manual, feita pelo cliente). Meta tags OG/Twitter já existem. Adicionar endpoints Flask para `/robots.txt` e `/sitemap.xml`, e Schema.org JSON-LD (SportsEvent) na landing.

## Notas técnicas importantes

- **Stack:** Flask + JSON storage (arquivo `tournament.json`) + HTML/JS/CSS vanilla + Docker via Coolify.
- **Arquivos principais:** `app.py` (~2280 linhas), `index.html` (~2150 linhas, é o /app), `landing.html` (~450 linhas, é a home).
- **Não quebrar:** Modo Ao Vivo (placar set-a-set com regras de vôlei: troca de lado no set 1→2, prompt no set 3, troca no 8º ponto do tie-break), algoritmo anti-sequência de jogos, horários automáticos, Modo Teste isolado, lista WhatsApp, tarja "TORNEIO ADIADO".
- **Regra permanente do cliente:** NUNCA propor testes em produção visível ao público. Se precisar testar em produção, criar caminho isolado/oculto (o Modo Teste já faz isso).
- **Senha admin:** `sampa2026`.
- **Repo:** `rodrigosvolei-creator/sampa-volleyball`. Deploy automático via Coolify no push.
- **Teste local:** rodar `python3 app.py` com `DATA_DIR` apontando pra pasta de teste, `SESSION_COOKIE_SECURE=0`, `INSCRICAO_ABRE_ISO` no passado pra liberar inscrições.

## Ordem sugerida de implementação

1. Migração de estrutura de dados (torneios como container) + migração dos dados existentes.
2. Backend: trocar rotas `<naipe>` → `<torneio_id>`, endpoint de mover equipe.
3. Frontend app: seletor de torneio, dropdown de inscrição com URL pré-seleção, filtro admin por torneio, mover equipe.
4. Frontend landing: URL de inscrição com torneio_id.
5. Modo Teste adaptado.
6. Testar tudo com 2 torneios simultâneos (o caso E1/E2).
7. Pendências: triangular + melhor de 5 + SEO (se houver tempo/escopo).
```
