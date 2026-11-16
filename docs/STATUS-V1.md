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
- Testes: 20 aprovados localmente; CI remoto pendente. Nenhum deploy ao Render.
