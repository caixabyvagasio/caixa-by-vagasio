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

## Ordem mestre — execução por fatias verificáveis (16/11/2026)
- Conferido o ponto de partida `a02eb05b2b22c7385a9c58110c5f605b15cc6945`: API FastAPI, PostgreSQL persistente, catálogo de segmentos e menu filtrado; sem modelagem de planos, filiais ou entitlements comerciais.
- Incremento desta etapa: catálogo da API passa a fornecer estados de recurso (`ENABLED`, `AVAILABLE`, `DISABLED`, `NOT_IMPLEMENTED`, `REQUIRES_CONFIGURATION`), além da disponibilidade e compatibilidade; respostas de segmento não elevam funções não implementadas a ativas. Na tela Perfil do negócio, lista de ferramentas mostra descrição, compatibilidade e estado, sem botão de ativação até existir elegibilidade de plano e fluxo de confirmação real.
- Leitura por código de barras segue não habilitada: embora produto já aceite código e busca de catálogo considere o campo, ainda não há entitlement/ativação com confirmação nem integração com balanças. Não anunciar o módulo como ativado.
- Validação local após esta etapa: 54 passed, 1 skipped (teste restrito a PostgreSQL de CI), 1 aviso de depreciação de TestClient/httpx. Verificação adicional: Python compila; referências de IDs da interface presentes; 15 formulários abertos/fechados balanceados; chaves CSS balanceadas.
- Próximas fatias: persistência de entitlements/planos/filiais antes de qualquer ativação opcional; fluxo de proposta/aceite transacional e trilha de ativação; depois módulos operacionais em vertical slices com APIs, UI, permissões e fluxos fim a fim. A ordem extensa dos seis segmentos não cabe num incremento único, e nenhum segmento pode ser declarado completo sem fluxos e testes de aceitação próprios.
- Não criar dados na empresa `vagasio-2026`, não habilitar custos externos nem promover para produção. PostgreSQL Render Free continua com data limite 16/12/2026, sem backup/PITR.
