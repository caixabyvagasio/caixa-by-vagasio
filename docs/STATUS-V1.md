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
