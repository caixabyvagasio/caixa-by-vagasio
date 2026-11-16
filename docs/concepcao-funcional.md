# Caixa by VagasIO — concepção funcional

## 1. Visão do produto

Plataforma SaaS de gestão comercial, vendas e PDV multissegmento. Um único sistema adapta módulos, menus e fluxos à realidade da empresa. Nesta fase, o produto terá repositório próprio no GitHub; domínio e migração para hospedagem definitiva ficam para depois.

**Princípio:** um sistema, diferentes segmentos, funcionalidades adaptadas a cada negócio.

## 2. Ambientes planejados

- **Portal Master:** administração global do produto, empresas, planos, módulos, monitoramento e auditoria; acesso de suporte a dados comerciais limitado, justificado e registrado.
- **Portal Empresarial:** gestão da empresa, filiais, produtos e serviços, estoque, clientes, colaboradores, financeiro e relatórios.
- **Terminal PDV:** ambiente rápido de venda, recebimento e operações de caixa.

## 3. Configuração inteligente por regras

O Assistente de Implantação deverá conduzir a configuração inicial: (1) identificação da empresa; (2) segmento principal; (3) características operacionais; (4) configuração sugerida; (5) revisão e ativação.

O motor de configuração é determinístico e auditável. Considera perfil operacional, segmento e respostas, plano, módulos contratados, permissões e filial. IA poderá futuramente sugerir configurações, mas não decidir ativação automaticamente. O CNAE pode sugerir segmento, mas a empresa confirma. Atividades mistas e módulos combináveis são suportados. Desativar módulo não apaga histórico.

Nenhum módulo pago ou serviço adicional será ativado sem consentimento explícito. O cliente deve distinguir recursos incluídos, opcionais com preço/condições e indisponíveis ou ainda não lançados. Limites do plano devem ser visíveis. Mudanças de plano seguem condições contratuais.

## 4. Núcleo universal

- Vendas: PDV, pedidos, orçamentos, descontos e cancelamentos controlados.
- Produtos e serviços: itens físicos, serviços, kits, combos, compostos, pesáveis e variantes; categorias, SKU/código de barras, unidade, custo, preço, margem, estoque e dados fiscais aplicáveis.
- Clientes: cadastro e histórico; crédito conforme permissões e regras.
- Estoque: movimentações, inventário, alertas e rastreabilidade.
- Financeiro: contas a pagar/receber, despesas, receitas, conciliação, fluxo de caixa e centros de custo.
- Operadores: usuários, perfis e permissões por ação.
- Relatórios: vendas, faturamento, margem e desempenho.
- Gestão: organizações, filiais, terminais, auditoria e configurações.
- Fiscal: configuração e emissão de documentos aplicáveis, somente após integração e validação legal/técnica.

## 5. Módulos especializados

Os segmentos são conjuntos combináveis de capacidades, não estruturas fechadas.

- **Supermercados/mercearias:** pesáveis, balanças compatíveis, etiquetas, códigos de barras, promoções, validade/lotes, múltiplos caixas e reposição.
- **Restaurantes/lanchonetes:** mesas, comandas, cardápio, adicionais, pedidos/cozinha, divisão de contas, delivery/retirada, fichas técnicas e insumos.
- **Vestuário/calçados:** grades de cor/tamanho, coleções, marcas, vendedores/comissões, trocas, devoluções, reservas e estoque por variante.
- **Salões/barbearias:** agenda, profissionais, serviços, comissões, pacotes, recorrência, produtos utilizados e histórico.
- **Materiais de construção:** orçamentos, venda por metragem, unidades, tabelas de preço, entregas, estoque e pedidos a fornecedores.
- **Prestação de serviços e outros:** catálogo modular sujeito a escopo, validação e lançamento próprios.

Padaria, por exemplo, poderá combinar varejo, alimentação e produção.

## 6. Portal Empresarial

Dashboard adaptado ao segmento, com indicadores úteis sem apresentar painéis irrelevantes. Cadastros e operações devem suportar matriz e filiais, fornecedores, compras, entradas/saídas, ajustes, perdas, devoluções e transferências rastreáveis. Saldo de estoque não deve ser alterado sem movimento justificável.

Separar valor vendido de valor efetivamente recebido, especialmente em cartão, parcelamento e pagamentos pendentes. Colaboradores e integrações futuras com VagasIO/Certus exigem escopo autorizado e dados segregados.

## 7. Terminal PDV

Fluxo planejado: operador autentica e abre caixa; identifica produtos; aplica descontos autorizados; escolhe uma ou mais formas de pagamento; confirma recebimento; emite documento aplicável; registra venda e movimentos de estoque.

Operações previstas: leitura/pesquisa de código de barras, quantidade e observações, limites de desconto, dinheiro/Pix/cartão e meios configurados, pagamentos mistos, troco, suspender/recuperar venda, cancelamento/devolução com rastreabilidade, abertura/sangria/suprimento/fechamento de caixa, comprovantes e histórico autorizado.

## 8. Confiabilidade e continuidade

Vendas idempotentes para impedir finalização duplicada. Pagamentos possuem estados explícitos (pendente, autorizado, confirmado, estornado, falho) e não são considerados concluídos sem confirmação do provedor. Operação offline só registra ações previamente autorizadas, com identificadores únicos, versões, idempotência e reconciliação. Fiscal em contingência depende das regras legais e técnicas; jamais presumir emissão ou pagamento confirmado. Integrações precisam de monitoramento, tentativas e reconciliação.

## 9. Segurança, dados e auditoria

Multiempresa/multifilial com isolamento obrigatório em API e banco. Permissões por perfil e ação, com perfis iniciais de proprietário, administrador, gerente, operador, estoquista, financeiro e vendedor, além de perfis personalizados. Operações sensíveis podem exigir autorização; registrar solicitante, autorizador, horário, contexto, empresa afetada, ação e resultado.

Proteger credenciais, dados pessoais e financeiros sob controles compatíveis com LGPD e requisitos dos provedores. Backups, testes de restauração, monitoramento e recuperação são requisitos operacionais. Acesso administrativo de suporte é limitado e auditável.

## 10. Integrações previstas (não presumir que existem)

Fiscal (NFC-e, NF-e, NFS-e conforme aplicabilidade), pagamentos (Pix, adquirentes, gateways, TEF), periféricos (leitores, impressoras, balanças, gavetas), comunicação (e-mail/notificações/WhatsApp por provedores), comércio digital e ecossistema VagasIO/Certus. Cada integração requer avaliação, credenciais, homologação e tratamento de falhas.

## 11. Comercialização

Assinaturas com planos, limites, teste, cupons, upgrade/downgrade e histórico de alterações. Catálogo Master de módulos define dependências, disponibilidade por plano/segmento e ativação controlada. Exemplo: comandas dependem de pedidos; cozinha é habilitada conforme operação de produtos preparados.

## 12. Estratégia de entrega

1. Fundação: arquitetura, banco, autenticação, organizações e filiais.
2. Personalização: segmentos, questionários, catálogo de módulos e motor de regras.
3. Gestão comercial: produtos, estoque, clientes, vendas e financeiro.
4. PDV: terminais, caixa e fluxos de venda.
5. Integrações: pagamentos, fiscal, periféricos e sincronização.
6. Especialização: restaurante, moda, serviços e demais segmentos.

V1 comercial começa pelo varejo geral e cresce gradualmente. Antes de cada liberação: testes funcionais, permissões, regras de negócio, carga e homologações aplicáveis. Não apresentar protótipo visual como serviço funcional ou operacional.
