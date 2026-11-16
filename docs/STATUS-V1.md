# Status de desenvolvimento — 2026-11-16

## Incremento de personalização por segmento (branch `development-v1`)
- Criado catálogo inicial de seis perfis: varejo, supermercados/mercearias, restaurante/lanchonete, vestuário/calçados, serviços e materiais de construção.
- Perguntas booleanas versionadas no servidor e respostas validadas por segmentos selecionados.
- Persistência de perfil por tenant, módulos recomendados por regras determinísticas, edição posterior sem apagar dados, auditoria append-only.
- UI empresarial para atividade principal, secundárias, perguntas e catálogo de sugestões.
- Os módulos marcados como sugestão não significam implementação, contratação ou disponibilidade operacional. Hardware, fiscal e pagamentos permanecem fora.
- Testes locais: 20 passaram; 1 aviso de depreciação de TestClient. CI e validação visual publicada pendentes.

## Plataforma conhecida
- Demo no Render Free, SQLite em `/tmp`, sem retenção confiável. Nenhum dado real.
- Portal empresarial inicial e PDV de varejo com dinheiro estão presentes. Portal Master, multifilial, PostgreSQL, compras, clientes, financeiro completo, fiscal, adquirência e offline não implementados.
- Não publicar este incremento em Render. Manter em branch isolada até revisão e aprovação de promoção; branch do serviço possui deploy manual/autodeploy desligado.


## Incremento de catálogo e estoque
- Campos de produto: categoria, código de barras por empresa (único), estoque mínimo; pesquisa aceita código de barras e endpoint de estoque crítico usa mínimos configurados.
- Migração SQLite idempotente adiciona os campos ao banco existente sem remover dados do catálogo. Estoque continua alterado somente por movimentos justificados/auditados.
- Portal empresarial ganhou campos correspondentes e sinalização de saldo baixo. Leituras físicas de scanner dependem de modelo/protocolo; leitores que digitam teclado podem alimentar a pesquisa, sem integração de hardware homologada.
- Testes: 22 aprovados localmente após a edição auditada; CI remoto pendente. Nenhum deploy ao Render. Nenhum deploy ao Render.

- Edição de produto implementada: valores anteriores e novos são gravados em evento de auditoria; mudanças respeitam tenant, papéis, unicidade de SKU e código.


## Incremento de compras e fornecedores
- Cadastro e listagem de fornecedores por empresa.
- Pedidos de compra com várias linhas, estados rascunho/enviado/recebimento parcial/recebido/cancelado.
- Recebimentos parciais ou integrais transacionais, idempotentes, com validação de excesso, entrada de estoque e auditoria. Pedido não altera saldo antes de recebimento. Nenhuma conta a pagar é criada: a obrigação financeira permanece fora do módulo até implementar termos/vencimentos e validação.
- Interface empresarial: cadastro, criação de pedido, marcar enviado, cancelamento, recebimento de quantidades e leitura do status.
- 27 testes locais passaram; CI remoto pendente. Sem deploy nem dados reais.

## Incremento de clientes e financeiro gerencial
- Cadastro e pesquisa de clientes por empresa, e-mail único opcional, campos de contato e observação; cliente pode ser associado a venda em dinheiro.
- API de detalhe do cliente exibe histórico de vendas confirmadas da própria empresa e total agregado; acesso cruzado é 404.
- Recebimento de compra pode incluir vencimento acordado. Quando informado, gera conta a pagar pelo custo das quantidades recebidas; sem vencimento, não inventa obrigação/parcelamento. Vencimento passado é rejeitado atomicamente.
- Tela financeira mostra vendas em dinheiro, contas a pagar abertas/vencidas e baixa manual auditada, identificada explicitamente como não confirmada pelo banco.
- Não há conciliação bancária, contas a receber a prazo, fluxo de caixa completo, DRE nem provedor financeiro. Valores do resumo são parciais, não faturamento contábil.
- Testes: 32 aprovados localmente; alerta conhecido de depreciação da dependência Starlette TestClient/httpx. Sem deploy.


## Incremento de cancelamento de vendas
- Gerente, administrador ou titular pode cancelar venda paga da própria empresa com justificativa obrigatória. Em uma transação: marca como cancelada, restaura estoque com movimentos `reversal`, estorna total no caixa e anexa auditoria; repetição é rejeitada.
- A devolução em espécie ao cliente continua manual; UI e resposta informam que não houve integração nem confirmação do pagamento.
- Testes locais: 34 passaram; UI de listagem agora apresenta venda cancelada e controle de cancelamento. CI remoto após commit pendente. Sem deploy.

## Incremento de histórico de estoque
- Endpoint com filtros por empresa/produto/tipo, paginação e total de movimentos.
- Nova tela do portal empresarial lista data, item, tipo, quantidade, saldo antes/depois, responsável e justificativa; filtro para entradas, saídas, vendas e estornos.
- Testes locais: 35 aprovados; UI e API estão apenas na branch de desenvolvimento até CI. Sem deploy.


## Correção de segurança do cancelamento em dinheiro
- Cancelamento agora exige caixa aberto e saldo esperado suficiente; lança estorno contra o caixa aberto corrente, sem reescrever caixa já fechado. Falta de caixa mantém a transação atômica: venda paga e estoque não muda. UI comunica baixa/devolução manual.
- Validação ampliada: 36 testes locais passaram; CI aguarda commit.

## Incremento de relatório de vendas
- API e tela de relatório por intervalo, agrupamento diário/mensal, contagem e totais separados para vendas pagas e canceladas. Datas e limites validados; limites de função e tenant. É operacional/gerencial, usa somente valores locais em dinheiro e UTC; não é relatório fiscal ou contábil.
- Testes locais: 37 aprovados. CI/deploy pendente.


## Publicação autorizada 2026-11-16
- O commit `9a407d6` (fornecedores/compras, clientes, financeiro inicial, cancelamento, histórico de estoque) foi publicado no serviço Render Free `caixa-by-vagasio-dev`; deploy `dep-dbtiossipgvc73a1npr0` ficou live.
- Correção que exige caixa aberto com saldo suficiente para cancelamento foi publicada no commit `be0694c`, deploy `dep-dbtiqtdbedkc73fvrac0` live.
- Relatório de vendas por dia/mês foi publicado no commit `276f22a`, deploy `dep-dbtit2etlv9c73acocl0` live, com CI success e 37 testes locais.
- Serviço está apontado para `development-v1`, autodeploy continua off, plano Free. Health respondeu 200; interface pública abriu e telas Compras e Movimentos de estoque foram carregadas na sessão de demonstração fictícia.
- Dados compartilhados/descartáveis com SQLite em `/tmp`; não usar para operação comercial nem dados reais. Pagamentos e emissão fiscal desligados.

## Correção após validação live
- A validação com requests concorrentes detectou `sqlite3.ProgrammingError` na liberação de conexão entre threads FastAPI; configurada conexão por request para permitir uso sequencial pela thread de cleanup e adicionado teste. 38 testes locais passaram.
- Corrigido também o título no topo para todas as telas incluídas. A correção está publicada no commit `9c85e7e`; health 200, interface `/` e relatório de vendas foram verificados após o deploy. O catálogo respondeu 500 de forma intermitente durante a validação; logs apontaram conexão SQLite encerrada de thread diferente. A revisão na correção live segue em andamento.

## Exportação de relatório
- Tela de relatório pode baixar CSV UTF-8 BOM, separado por ponto e vírgula, com linhas diárias/mensais e totais pagos/cancelados; exportação só fica habilitada depois de consulta bem-sucedida.
- Pós-correção live: /health 200 repetido; acessos autenticados de demonstração a `/v1/products?limit=200` retornaram 200, sem novos traces de erro na janela consultada. A UI de relatório foi verificada por navegador.
- Testes locais após CSV: 38 aprovados; CI/deploy desta pequena atualização pendentes.
