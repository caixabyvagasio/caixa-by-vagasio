# Progresso — Caixa by VagasIO

## Base e auditoria anterior
- Arquitetura de base auditada: FastAPI/Python, SQLite inicial, HTML/CSS/JS, API tenant-scoped; primeiras preocupações foram signup aberto em demo, RBAC/PII, armazenamento efêmero e feature catalog sem módulos reais.
- Hardening anterior: `cc926672...` bloqueou signup em demo e minimizou PII; `292e336...` limitou escrita de produtos ao estoque; `fe4faba...` reforçou RBAC, privacidade e valuation. CI antigo verificado no histórico do repo.
- Bloco financeiro inicial no commit `ef082c4a...`; migrations SQLite versão 1–4 em `9f260800...`; testes e CI sucessos, detalhes nos docs históricos e `FINANCEIRO-PARCIAL-V1.md`.

## PostgreSQL persistente no plano autorizado
- Criado Render Postgres Free, Ohio, PG18, database `caixa_vagasio`, expiração 16/12/2026. Vazio: dados do SQLite temporário anterior não puderam ser recuperados.
- Adaptador psycopg2/Postgres e workflow de integração real adicionados; primeira CI encontrou diferenças SQL e foram corrigidas em `b0459941fe9b00124c6edb653ece012130346114`.
- CI Postgres `43961405960` success, CI comum `43961406033` success.
- Aplicação conectada à URL interna via env secreto; `CAIXA_REQUIRE_POSTGRES=true`, modo demo desligado e signup autorizado pelo guard de PostgreSQL. Deploy live `dep-dbtngvt6e07s73akjpfg`, commit `b0459941...`; UI status dinâmico publicada em `dep-dbtnkpm7bikc73f690m0`, commit `ac5248ee7cf212a62b5f086c5069c7a82e62da26`.
- Health confirmou `storage=postgresql`, demo false, signup enabled. Signup/login fictício 201/200 e `/v1/me` confirmou tenant. Conta artificial conseguiu autenticar após redeploy. Teste separado GET users retornou apenas seu titular; browser autêntico mostrou a conta do tenant, removendo suspeita de vazamento.
- CI do commit UI: `43963765433` (PG) success, `43963765448` success.
- Testes locais desse commit: 51 passed, 1 skipped (teste que necessita Postgres CI), 1 warning deprecação Starlette TestClient/httpx. Browser verificou login e tela Equipe/usuários da empresa fictícia.

## Render Free e prazos
- O banco gratuito expira em 30 dias e o plano não oferece backups/PITR. Data emitida pela API Render: 16/12/2026 20:59 UTC. Conforme orientação do usuário, estamos usando Free agora; atualizar/migrar antes do vencimento é indispensável para continuidade.
- Web service continua Free e dorme por inatividade; reinício deve preservar os registros por causa do PostgreSQL agora ligado.
- Nenhuma connection string ou credencial é documentada. Dados reais não foram usados no smoke.

## Pendências da plataforma completa
- Não há ainda cópia/backup recuperável verificada; gerar export e testar restauração antes de usar dados sensíveis/comerciais.
- Pagamentos, fiscal e integrações externas continuam desligados; catálogo de segmentos não ativa recursos ausentes.
- Portal Master, filiais, planos/entitlements funcionais, recebíveis, conciliação, DRE, módulos especializados e offline seguem em aberto.
- Persistência agora testada após um redeploy de app em conta fictícia, mas sem restore testado, sem garantias comerciais e Free tem limite temporal. Não chamar de plataforma final homologada.
