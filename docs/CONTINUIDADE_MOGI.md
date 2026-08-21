# Continuidade da consulta fiscal de Mogi

Este é o ponto de passagem para quem continuar a parte fiscal. Eu registrei aqui somente o que foi confirmado na fonte municipal e no código atual, sem apresentar uma integração ainda inexistente como concluída.

## O que já está pronto

- cadastro público e histórico venal vinculados por `cadastro_id`;
- contexto territorial aproximado vinculado por `id_local`;
- tabela `fiscal_status` no checkpoint;
- importação de situação fiscal por CSV, TXT delimitado ou JSON;
- vínculo da situação importada aos cadastros públicos;
- exportação separada em `resultados/mogi/situacao_fiscal_importada.csv`;
- testes que preservam a separação entre cadastro, valor venal e situação fiscal.

## Portal investigado

Página oficial consultada:

- [Edital 4 — Transação Fiscal](https://servicos.mogidascruzes.sp.gov.br/tbw/loginWeb.jsp?execobj=i129ServicosWebParcMogiano&edital=4)

A página oferece três tipos de cadastro: CCM/Inscrição Municipal, CPF/CNPJ para CRC e Imobiliário. No tipo Imobiliário, o JavaScript deixa visível a inscrição do imóvel e o texto da imagem. O envio segue para a operação municipal `ParcelamentoPesquisaDebitoWEB`.

O próprio texto da página limita o resultado a débitos tributários e não tributários inscritos na Dívida Ativa Municipal dentro da transação fiscal. Portanto, ausência de resultado nessa tela deve ser registrada como `nenhum_debito_elegivel_localizado_edital_4`, e nunca como quitação geral do imóvel.

## Identificador exigido pelo portal

A máscara atual aceita:

- predial: `00.000.000.000-0`;
- territorial: `00.000.000-00`.

O `id_local` preservado na base pública possui onze dígitos em casos como `01-004811-001`, mas o projeto não possui uma regra oficial validada para produzir o dígito verificador final. Antes de automatizar o preenchimento, o desenvolvedor deve obter o identificador completo em documento autorizado ou localizar uma regra municipal documentada. Não se deve testar combinações de dígitos contra o portal.

## Próxima implementação

1. Obter uma resposta real de consulta autorizada e salvar uma cópia local em `capturas_locais/`.
2. Identificar no HTML somente os campos fiscais necessários e os estados de erro.
3. Criar um parser independente da automação do navegador.
4. Validar o parser repetidamente sobre a captura local.
5. Adaptar a saída para os campos já aceitos por `import_fiscal_status`.
6. Importar no checkpoint e confirmar o vínculo com o cadastro correto.
7. Exibir na ficha a fonte, a data, o escopo do resultado e a situação encontrada.
8. Fazer um único teste integrado autorizado antes da entrega.

O fluxo no navegador deve preencher a inscrição e pausar para a validação humana exigida pela página. Depois da resposta, o parser e a importação podem operar automaticamente.

## Campos aceitos na integração atual

| Campo | Finalidade |
|---|---|
| `inscricao_imobiliaria` | vínculo com o cadastro público |
| `competencia` | exercício ou referência do resultado |
| `possui_pendencia` | indicador normalizado |
| `status_pendencia` | texto fiel ao resultado |
| `valor_total_pendencia` | total agregado, quando publicado |
| `quantidade_pendencias` | quantidade agregada, quando publicada |
| `fonte_consulta` | portal, certidão ou órgão emissor |
| `data_consulta` | momento em que o resultado foi obtido |
| `numero_documento` | protocolo ou certidão, quando existir |

Dados de boleto e instrumentos de pagamento não pertencem a essa tabela. Qualquer tratamento de dados pessoais fornecidos pelo próprio cliente deve permanecer fora da base pública do mapa e depender de um módulo privado com autorização, controle de acesso, auditoria e retenção definidos pelo responsável jurídico.

## Critérios para considerar a etapa concluída

- parser validado contra resposta municipal real;
- distinção clara entre Dívida Ativa do Edital 4, certidão e posição financeira;
- inscrição completa validada sem tentativa de adivinhação;
- vínculo fiscal testado sem juntar cadastros distintos;
- origem, data e escopo visíveis na interface;
- nenhuma resposta vazia apresentada como “sem débitos”;
- testes automatizados e compilação de sintaxe aprovados.
