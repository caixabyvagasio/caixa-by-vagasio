# Status de desenvolvimento — 2026-11-16

## Incremento de personalização por segmento (branch `development-v1`)
- Criado catálogo inicial de seis perfis: varejo, supermercados/mercearias, restaurante/lanchonete, vestuário/calçados, serviços e materiais de construção.
- Perguntas booleanas versionadas no servidor e respostas validadas por segmentos selecionados.
- Persistência de perfil por tenant, módulos recomendados por regras determinísticas, edição posterior sem apagar dados, auditoria append-only.
- UI empresarial para atividade principal, secundárias, perguntas e catálogo de sugestões.
- Os módulos marcados como sugestão não significam implementação, contratação ou disponibilidade operacional. Hardware, fiscal e pagamentos permanecem fora.
- Testes locais: 17 passaram; 1 aviso de depreciação de TestClient. CI e validação visual publicada pendentes.

## Plataforma conhecida
- Demo no Render Free, SQLite em `/tmp`, sem retenção confiável. Nenhum dado real.
- Portal empresarial inicial e PDV de varejo com dinheiro estão presentes. Portal Master, multifilial, PostgreSQL, compras, clientes, financeiro completo, fiscal, adquirência e offline não implementados.
- Não publicar este incremento em Render. Manter em branch isolada até revisão e aprovação de promoção; branch do serviço possui deploy manual/autodeploy desligado.
