# Progresso de execução — auditoria e correções prioritárias

Data: 2026-11-16. Branch `development-v1`; base do ciclo `ff72d7a2a3606eec2cdcd188b3a2ad313db17955`.

## Concluído
- Auditoria da API, telas, schema/migrações inline, configurações, deploy e testes. Inventário e achados priorizados em `PLANO-AUDITORIA-E-EXECUCAO.md`.
- Testes originais do HEAD: 39 passed. Smoke público de página e health: HTTP 200. Conferida tela do relatório de estoque.
- Commit de hardening `cc9266726412e5db42b14347d33c5a7c569b64c6` na branch de desenvolvimento; CI run `43913857996` success. Cadastro público de empresa é bloqueado durante modo demo mesmo se signup estiver configurado como true. Os endpoints de clientes agora têm RBAC e minimizam o retorno do operador; somente owner/admin/manager veem PII/histórico; operador vê nomes para lookup PDV e só pode cadastrar nome; estoque sem leitura de clientes.
- Suíte sobre esse estado: 41 passed, 1 warning depreciação TestClient/httpx.
- Na revisão aprofundada foi visto que papel `stock` podia editar preço e custo via PUT/POST de produtos. Corrigido no worktree seguinte: somente owner/admin/manager podem criar/editar produto; adicionado teste negativo.
- Suíte do worktree após esse ajuste adicional: 42 passed, 1 warning.

## Estado Git/deploy
- O commit `cc92667` já foi pushed para `development-v1`, CI success; **não foi deployed**. O serviço público permanece com HEAD `ff72d7a` e autodeploy off, em respeito à instrução de não publicar sem autorização.
- O bloqueio do signup durante demo e PII endpoints estão no commit acima. Restrição de editar preço/custo para estoque é alteração local ainda sem commit; precisa commit + CI. Não publicar sem autorização.
- O sistema segue Render Free + SQLite `/tmp`, demo pública compartilhada, sem backup durável; não é para dados reais.

## Próximas tarefas
1. Commit e CI do RBAC de produto; atualizar plano/progresso e revisar mais cada endpoint de permissão.
2. Cobertura de logs/auditoria, segurança de conteúdo/PII, acessibilidade e testes E2E.
3. Arquitetar/implementar migrations formais e suporte PostgreSQL em branch, testar em banco local/CI sem provisionar serviço pago.
4. Prosseguir Portal Master, entitlements verdadeiros e módulos e fluxos pendentes sem afirmar completude nem publicar no ambiente público.
5. Integrações fiscais/pagamentos requerem contratos, credenciais e homologação; infraestrutura persistente/backups requerem provider/custo aprovado. Não inventar sucesso nem contratar.
