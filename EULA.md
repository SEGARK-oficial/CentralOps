# CONTRATO DE LICENÇA DE USUÁRIO FINAL ("EULA")
## CentralOps — Plataforma de Operações de Segurança

**Versão:** 2.0
**Data de vigência:** 14 de maio de 2026
**Última atualização:** 14 de maio de 2026
**Licenciante:** Dathan Vitor Santana da Nobrega, pessoa física inscrita no CPF/MF, residente no Brasil ("Licenciante")
**Software licenciado:** CentralOps — Plataforma de Operações de Segurança (Security Operations Center Platform), incluindo o backend em Python compilado, frontend em React/Vite, imagens Docker, scripts auxiliares, documentação técnica e quaisquer atualizações, correções, módulos adicionais ou trabalhos derivados disponibilizados pelo Licenciante ("Software")

---

## ⚖️ AVISO LEGAL IMPORTANTE

> **ESTE DOCUMENTO É UM TEMPLATE DE EULA PREPARADO PARA A DISTRIBUIÇÃO INICIAL DO SOFTWARE.** Embora elaborado com base em precedentes da indústria (Elastic License v2, Microsoft Container EULA, Docker Subscription Agreement), legislação brasileira aplicável (Lei nº 9.609/98 — Lei do Software; Lei nº 13.709/2018 — LGPD; Lei nº 10.406/2002 — Código Civil) e melhores práticas para licenciamento de software proprietário, **ele NÃO SUBSTITUI revisão jurídica por advogado(a) regularmente inscrito(a) na OAB**. Antes de utilizar este EULA em qualquer relação comercial efetiva — especialmente em vendas, distribuição em larga escala, ou disputas — submeta o texto a revisão profissional adaptada ao seu modelo de negócio, jurisdição(ões) de atuação e perfil de Licenciados.
>
> Cláusulas específicas que exigem atenção particular do(a) advogado(a) revisor(a): (i) limitação de responsabilidade (Seção 16); (ii) foro e lei aplicável (Seção 22); (iii) tratamento de dados pessoais e LGPD (Seção 11); (iv) renúncia a ações coletivas, se aplicável ao seu mercado; (v) export control caso haja distribuição fora do Brasil; (vi) cláusulas de auditoria diante do CDC se houver consumidores pessoas físicas.

---

## SUMÁRIO

1. Preâmbulo e contexto
2. Aceitação do contrato
3. Definições
4. Concessão de licença
5. Restrições de uso
6. Reserva de direitos
7. Atualizações, novas versões e descontinuidade
8. Suporte técnico (Lei 9.609/98, art. 8º)
9. Propriedade intelectual
10. Confidencialidade
11. Dados pessoais e conformidade com a LGPD
12. Componentes de software de terceiros (open source)
13. Conformidade legal e controle de exportação
14. Auditoria
15. Garantias e declarações
16. Limitação de responsabilidade
17. Indenização
18. Prazo e rescisão
19. Caso fortuito e força maior
20. Cessão e sucessão
21. Comunicações e notificações
22. Lei aplicável e foro
23. Disposições gerais
24. Glossário rápido
25. Anexo I — Termos específicos para Imagem Docker pública
26. Anexo II — Tratamento de dados pessoais (DPA)

---

## 1. PREÂMBULO E CONTEXTO

1.1. O CentralOps é uma plataforma de operações de segurança (SOC) que integra, normaliza e opera dados de fornecedores terceiros de cibersegurança (incluindo, sem limitação, Sophos Central, Microsoft Defender, NinjaOne e Wazuh), oferecendo telemetria, dashboards, ações remediadoras (block IP, block hash) e dispatch para SIEM downstream.

1.2. O Software é **proprietário**. Embora o Licenciante possa, a seu exclusivo critério, disponibilizar imagens Docker e código-fonte em repositórios públicos (por exemplo, ``ghcr.io`` ou GitHub) por motivos de conveniência operacional dos Licenciados legítimos, **isto não constitui licença ampla de uso, redistribuição, decompilação ou exploração comercial**. Toda interação com o Software fica subordinada a este EULA.

1.3. Substituição de licença anterior. Versões anteriores do repositório do Software podem ter sido publicadas sob a licença Apache 2.0. A partir da Data de Vigência indicada no cabeçalho, novas versões do Software passam a ser licenciadas exclusivamente nos termos deste EULA. Esta substituição não retroage sobre cópias previamente obtidas sob a Apache 2.0, mas se aplica a qualquer nova versão, *commit*, *release*, imagem Docker, atualização, *patch* ou módulo posterior à Data de Vigência.

---

## 2. ACEITAÇÃO DO CONTRATO

2.1. **Vinculação por uso.** Você (pessoa física ou jurídica, doravante "Licenciado") aceita expressa e integralmente todos os termos deste EULA ao realizar qualquer um dos seguintes atos:

  (a) baixar (``git clone``, ``docker pull``, *download* direto) qualquer porção do Software;
  (b) instalar, executar, hospedar ou orquestrar o Software ou qualquer Imagem do Software em qualquer ambiente;
  (c) acessar, ler ou analisar o código-fonte ou os artefatos compilados do Software;
  (d) integrar o Software a qualquer outro sistema, produto ou serviço;
  (e) modificar arquivos de configuração ou *templates* fornecidos com o Software.

2.2. **Aceitação por representante.** Se o Licenciado for pessoa jurídica, o ato de aceitação por meio do(a) representante, funcionário(a), contratado(a) ou agente que pratique qualquer dos atos da Seção 2.1 vincula a pessoa jurídica, independentemente de assinatura formal, conforme art. 107 do Código Civil Brasileiro (forma livre).

2.3. **Capacidade.** O Licenciado declara possuir capacidade jurídica plena para celebrar este contrato. Se pessoa jurídica, declara que o representante que aceita o EULA tem poderes societários suficientes.

2.4. **Recusa.** Se o Licenciado não concorda com qualquer cláusula deste EULA, deve imediatamente: (i) cessar todo uso do Software, (ii) remover toda cópia do Software (incluindo imagens Docker) de seus sistemas, e (iii) destruir cópias físicas, se existentes. A não cessação imediata constitui aceitação tácita.

---

## 3. DEFINIÇÕES

Para os fins deste EULA, os termos abaixo, quando iniciados com letra maiúscula, terão os seguintes significados:

3.1. **"Software"** — conforme definido no cabeçalho.

3.2. **"Imagem"** ou **"Imagem do Software"** — qualquer artefato de *container* (Docker, OCI ou similar) que contenha, total ou parcialmente, o Software, hospedado pelo Licenciante em registry público (por exemplo, *GitHub Container Registry*) ou privado.

3.3. **"Código-Fonte"** — os arquivos textuais ``.py``, ``.ts``, ``.tsx``, ``.yml``, ``.json``, ``.html``, ``.sh`` e demais artefatos legíveis por humano que componham o Software.

3.4. **"Artefatos Compilados"** — os arquivos ``.so`` resultantes da compilação em Cython, *bundle* JavaScript gerado por Vite, *bytecode* Python, e quaisquer outros artefatos binários ou compilados produzidos a partir do Código-Fonte.

3.5. **"Documentação"** — manuais, *runbooks*, comentários técnicos, *README*, documentação de API, ADRs e qualquer material descritivo do Software disponibilizado pelo Licenciante.

3.6. **"Uso Interno"** — execução do Software pelo próprio Licenciado, seus empregados (CLT, PJ, estagiários) e contratados sujeitos a obrigação equivalente de confidencialidade, exclusivamente para apoio às operações de segurança internas do Licenciado e/ou de empresas pertencentes ao mesmo grupo econômico.

3.7. **"Serviço Comercial"** — qualquer oferta, com ou sem contraprestação financeira, em que o Software, qualquer porção substancial dele, ou trabalho substancialmente derivado, seja disponibilizado a terceiros como serviço gerenciado, *hosted service*, *Software as a Service* (SaaS), *Managed Detection and Response* (MDR), terceirização de SOC ou qualquer modelo equivalente em que terceiros se beneficiem materialmente das funcionalidades do Software.

3.8. **"Engenharia Reversa"** — qualquer técnica destinada a obter o Código-Fonte, a estrutura interna, os algoritmos ou os segredos comerciais do Software a partir dos Artefatos Compilados, incluindo, sem limitação, decompilação, desmontagem, *disassembly*, depuração simbólica, extração de strings com intenção de reconstrução, análise por ferramentas de *reverse engineering* (IDA Pro, Ghidra, Binary Ninja, *Cython decompilers*) ou análise estática/dinâmica equivalentes.

3.9. **"Componentes de Terceiros"** — os softwares de terceiros, em particular *open source*, dos quais o Software depende em tempo de execução ou compilação (por exemplo: FastAPI, SQLAlchemy, Pydantic, Celery, Cython, React, Vite, nginx, PostgreSQL, Redis), cada qual sujeito à sua respectiva licença original.

3.10. **"Dados Pessoais"** — toda informação relacionada a pessoa natural identificada ou identificável, na forma do art. 5º, I, da LGPD.

3.11. **"Tratamento"** — toda operação realizada com Dados Pessoais, na forma do art. 5º, X, da LGPD.

3.12. **"Controlador"** e **"Operador"** — conforme definidos no art. 5º, VI e VII, da LGPD.

3.13. **"Trabalho Derivado"** — qualquer obra que incorpore, modifique, traduza, adapte, transforme ou se baseie de forma substancial no Software, incluindo *forks*, *patches* aplicados, recompilações com lógica alterada e *plugins* que reutilizem código não público.

3.14. **"Validade Técnica"** — período durante o qual o Licenciante presta suporte e mantém compatibilidade da versão do Software entregue, na forma do art. 8º da Lei 9.609/98 (vide Seção 8).

3.15. **"Dia Útil"** — qualquer dia de segunda a sexta-feira, excluídos feriados nacionais brasileiros e o feriado do município de domicílio do Licenciante.

---

## 4. CONCESSÃO DE LICENÇA

4.1. **Licença concedida.** Sujeita ao cumprimento integral deste EULA, o Licenciante concede ao Licenciado licença **não exclusiva, não transferível, não sublicenciável, revogável e gratuita** para:

  (a) **Baixar e instalar** a Imagem do Software a partir do registry oficial do Licenciante;
  (b) **Executar** o Software em ambientes controlados pelo Licenciado, em quantidade ilimitada de instâncias e usuários internos, exclusivamente para Uso Interno;
  (c) **Ler** o Código-Fonte e a Documentação distribuídos com o Software para fins de: compreensão de integrações, revisão de segurança pelo time do Licenciado, depuração de problemas operacionais, e diligência técnica em processos de aquisição corporativa;
  (d) **Modificar** os arquivos de configuração (``docker-compose.yml``, variáveis de ambiente, *templates* nginx, regras de mapping definidas em interface), bem como criar mappings, regras de normalização e dashboards adicionais, observada a Seção 5;
  (e) **Realizar cópias de segurança** (*backups*) das Imagens e Códigos-Fonte, exclusivamente para fins de recuperação de desastre do próprio ambiente do Licenciado.

4.2. **Escopo geográfico.** A licença é concedida em base mundial, observadas as Seções 13 (Controle de Exportação) e 22 (Lei Aplicável e Foro).

4.3. **Limitação técnica de validade.** A licença vige durante o período de Validade Técnica de cada versão entregue, conforme Seção 7.

4.4. **Direitos não concedidos.** Todos os direitos não expressamente concedidos nesta Seção 4 são reservados ao Licenciante.

---

## 5. RESTRIÇÕES DE USO

O Licenciado **NÃO PODERÁ**, por si, seus empregados, contratados, agentes ou qualquer pessoa sob seu controle, direta ou indiretamente:

5.1. **Engenharia reversa.** Aplicar Engenharia Reversa sobre os Artefatos Compilados, ressalvadas exclusivamente as hipóteses imperativamente autorizadas pela Lei nº 9.609/98, art. 6º, IV (interoperabilidade com outros programas independentes), observados todos os requisitos legais ali estabelecidos. Em qualquer caso, o Licenciado deverá notificar previamente o Licenciante e oferecer alternativa contratual de obtenção das informações de interoperabilidade.

5.2. **Redistribuição.** Redistribuir, sublicenciar, vender, alugar, ceder, emprestar, transferir, hospedar para terceiros, espelhar em registry próprio, ou de qualquer outra forma disponibilizar o Software, qualquer porção substancial, ou qualquer Trabalho Derivado a terceiros, com ou sem contraprestação.

5.3. **Serviço Comercial.** Operar Serviço Comercial que incorpore o Software ou qualquer porção substancial dele sem celebração de instrumento contratual separado, assinado pelas partes, com termos comerciais específicos. A leitura conjunta deste EULA com a definição da Seção 3.7 é decisiva: **se você presta SOC-as-a-Service ou MDR para terceiros usando o Software, você precisa de uma licença comercial específica.**

5.4. **Remoção de avisos.** Remover, alterar, ofuscar, encobrir ou suprimir quaisquer avisos de copyright, marca registrada, atribuição, *watermarks* ou outras indicações de propriedade contidos no Software, nos Artefatos Compilados ou na Documentação.

5.5. **Treinamento de modelos competidores.** Utilizar o Software, seus *logs*, *datasets* de treino, regras de normalização, ou qualquer artefato técnico do Licenciante para desenvolver, treinar, ajustar (*fine-tune*) ou aperfeiçoar produto ou serviço substancialmente competitivo com o Software, incluindo modelos de aprendizado de máquina destinados a SOC ou *threat intelligence*.

5.6. **Burla de controles técnicos.** Contornar, desativar ou comprometer qualquer medida técnica do Software destinada a proteger a propriedade intelectual do Licenciante, incluindo, sem limitação: validação de licença, telemetria de uso, ofuscação de código, integridade binária, *checksums* e mecanismos antimanipulação.

5.7. **Uso ilícito.** Utilizar o Software em violação de qualquer norma legal aplicável, incluindo, sem se limitar a: legislação trabalhista, fiscal, anticorrupção (Lei nº 12.846/2013 — Lei Anticorrupção), antiterrorismo, controles internacionais de exportação, sanções econômicas, proteção de dados (LGPD, GDPR, CCPA), propriedade intelectual ou concorrencial.

5.8. **Decompilação de artefatos OSS embarcados.** Aplicar Engenharia Reversa também sobre Componentes de Terceiros embarcados, salvo na medida em que a licença original do componente expressamente o permita.

5.9. **Republicação de avisos de segurança.** Republicar, antes de janela razoável de divulgação coordenada (90 dias mínimos, ou prazo definido em diretrizes do Licenciante), qualquer vulnerabilidade descoberta no Software. A janela de divulgação se aplica salvo quando vulnerabilidade já tenha sido publicamente divulgada por terceiro independente.

5.10. **Marca e nome.** Utilizar o nome "CentralOps", logos, *trade dress* ou marcas associadas ao Software para sugerir endosso, parceria ou afiliação não existente, ou de modo que possa causar confusão no mercado.

5.11. **Acesso por concorrentes.** Permitir acesso ao Código-Fonte ou aos Artefatos Compilados a concorrentes diretos do Licenciante (incluindo, sem limitação, outros fornecedores de plataforma SOC, MDR ou XDR comercial) sem prévio consentimento por escrito do Licenciante.

---

## 6. RESERVA DE DIREITOS

6.1. O Software é **licenciado, não vendido**. O Licenciado não adquire qualquer direito de propriedade sobre o Software, sua arquitetura, seus algoritmos, segredos comerciais, marcas registradas, designs, *know-how* ou Trabalhos Derivados desenvolvidos pelo Licenciante.

6.2. Qualquer melhoria, sugestão, *bug report*, ideia, *feedback* ou contribuição apresentada pelo Licenciado ao Licenciante poderá ser livremente incorporada ao Software, sem contraprestação, atribuição obrigatória ou direito à exclusividade, observado o art. 4º da Lei 9.609/98.

6.3. Trabalhos Derivados criados pelo Licenciado em violação à Seção 5 deste EULA, ou que incorporem porções substanciais do Software, **pertencem ao Licenciante**, sem prejuízo das medidas judiciais cabíveis pela violação contratual.

---

## 7. ATUALIZAÇÕES, NOVAS VERSÕES E DESCONTINUIDADE

7.1. **Direito a atualizações.** O Licenciado tem direito de receber, durante a Validade Técnica da versão em uso, correções de segurança críticas, correções de defeitos materiais e atualizações menores publicadas pelo Licenciante, na forma e canal que este julgar adequado.

7.2. **Validade técnica.** Cada versão *major* (X.0.0) do Software possui Validade Técnica mínima de **12 (doze) meses** a contar de sua publicação, conforme exigência do art. 8º da Lei 9.609/98. O Licenciante poderá estender essa Validade Técnica unilateralmente, mas nunca reduzi-la abaixo do prazo legal mínimo.

7.3. **Versões novas.** Versões *major* novas (X+1.0.0) constituem produto distinto e podem ser licenciadas sob termos atualizados deste EULA. O uso de versão nova após sua publicação implica aceitação dos termos atualizados, na forma da Seção 23.4.

7.4. **Descontinuidade do Software.** O Licenciante poderá descontinuar o Software, no todo ou em parte, mediante aviso público com antecedência mínima de **90 (noventa) dias**, publicado no repositório oficial ou em canal equivalente. Após a descontinuidade, a obrigação de suporte (Seção 8) cessa, e a presente licença passa a ser concedida em base "AS IS" pelo período remanescente em que o Licenciado permanecer usando a última versão suportada.

7.5. **Compatibilidade.** O Licenciante envidará esforços razoáveis, mas não garante, compatibilidade entre versões. *Breaking changes* serão sinalizados em ``CHANGELOG`` e/ou notas de *release*.

---

## 8. SUPORTE TÉCNICO (Lei 9.609/98, art. 8º)

8.1. **Cumprimento da obrigação legal.** Em cumprimento ao art. 8º da Lei 9.609/98, o Licenciante prestará suporte técnico relativo ao funcionamento adequado do Software, observadas as suas especificações, durante o período de Validade Técnica.

8.2. **Canais e SLA.** O suporte técnico será prestado:
  (a) Por meio de *issue tracker* no repositório público do Software, em prazo razoável (*best effort*);
  (b) Sem garantia de tempo de resposta (SLA) específico, salvo se previsto em contrato comercial separado.

8.3. **Escopo do suporte.** O suporte gratuito abrange exclusivamente: (i) defeitos no funcionamento do Software em relação às suas especificações declaradas; (ii) orientações básicas de instalação e configuração documentadas. **Não abrange**: customização, integração com ambientes específicos, treinamento, recuperação de dados, *consulting*, ou auditoria de segurança do ambiente do Licenciado, os quais demandam contratação separada.

8.4. **Contrato comercial de suporte.** O Licenciado interessado em suporte comercial com SLA, *response time* garantido, *hotline* ou serviços profissionais deve celebrar contrato específico com o Licenciante.

---

## 9. PROPRIEDADE INTELECTUAL

9.1. **Titularidade.** O Software, incluindo todos os elementos protegidos por direito autoral, segredos industriais, *trade secrets* e *know-how*, é de titularidade exclusiva do Licenciante, nos termos da Lei 9.609/98 (Lei do Software), Lei 9.610/98 (Lei de Direitos Autorais) e Lei 9.279/96 (Lei de Propriedade Industrial), conforme aplicável.

9.2. **Marcas.** "CentralOps" e quaisquer logos, ícones e *trade dress* associados são marcas (registradas ou em processo de registro) do Licenciante. O uso não autorizado dessas marcas é proibido.

9.3. **Patentes.** Eventuais invenções incorporadas ao Software poderão ser objeto de proteção patentária pelo Licenciante. Nada neste EULA concede ao Licenciado licença sobre patentes do Licenciante, exceto o estritamente necessário para o uso autorizado do Software.

9.4. **Notificação de violação.** O Licenciado deverá notificar o Licenciante em até **5 (cinco) Dias Úteis** caso tome conhecimento de qualquer reivindicação de terceiros sobre direitos de propriedade intelectual relativos ao Software.

---

## 10. CONFIDENCIALIDADE

10.1. **Informações Confidenciais.** Constituem informações confidenciais do Licenciante: o Código-Fonte não-público, arquitetura interna não documentada publicamente, algoritmos proprietários, regras de normalização, *roadmap*, métricas de uso, *benchmarks* internos, comunicações privadas e qualquer outro material claramente identificado como confidencial ou cuja natureza confidencial seja razoavelmente inferível.

10.2. **Obrigações do Licenciado.** O Licenciado deverá: (i) manter sigilo absoluto sobre as Informações Confidenciais; (ii) restringir o acesso a empregados/contratados com necessidade legítima e vinculados a obrigação equivalente de sigilo; (iii) empregar grau de cuidado não inferior ao que dispensa às suas próprias informações de mesma sensibilidade, sempre observado padrão razoável de segurança da informação.

10.3. **Exceções.** Não são confidenciais informações que: (i) sejam ou se tornem de domínio público sem violação deste EULA; (ii) eram de conhecimento prévio do Licenciado por meio lícito; (iii) sejam recebidas legitimamente de terceiro sem dever de sigilo; (iv) sejam desenvolvidas independentemente sem uso de Informação Confidencial; (v) devam ser reveladas por ordem judicial ou de autoridade competente, hipótese em que o Licenciado notificará o Licenciante imediatamente para que este busque remédios legais.

10.4. **Sobrevida.** A obrigação de confidencialidade sobrevive à extinção deste EULA pelo prazo de **5 (cinco) anos** ou enquanto a informação preservar caráter confidencial, prevalecendo o que for maior.

---

## 11. DADOS PESSOAIS E CONFORMIDADE COM A LGPD

11.1. **Premissa.** O CentralOps é uma plataforma operada **pelo Licenciado em sua própria infraestrutura** (*self-hosted*). O Licenciante **não tem acesso aos Dados Pessoais** que circulam pelo Software no ambiente do Licenciado, salvo se Dados Pessoais forem voluntariamente transmitidos ao Licenciante por meio de canais de suporte ou *bug reports* (item 11.4).

11.2. **Papéis sob a LGPD.**
  (a) **Em relação aos Dados Pessoais tratados pelo Software no ambiente do Licenciado:** o Licenciado é o **Controlador** (art. 5º, VI, LGPD), com plena responsabilidade pelas decisões referentes ao Tratamento. O Licenciante **não atua como Operador** nesses Dados Pessoais, pois não realiza Tratamento em nome do Licenciado fora do ambiente do próprio Licenciado.
  (b) **Em relação aos Dados Pessoais que o Licenciado eventualmente compartilhe com o Licenciante (suporte, *bug reports*, *crash dumps*):** o Licenciante atua como **Operador** desses Dados específicos, tratando-os exclusivamente para a finalidade de resolução do incidente reportado, conforme detalhado no **Anexo II — Tratamento de Dados Pessoais (DPA)**.

11.3. **Obrigações do Licenciado como Controlador.** O Licenciado é integralmente responsável por: (i) bases legais de Tratamento (art. 7º da LGPD); (ii) atendimento aos direitos de titulares (arts. 17 a 22); (iii) políticas de retenção; (iv) relatórios de impacto à proteção de Dados Pessoais; (v) nomeação de DPO quando exigível; (vi) notificações à ANPD e a titulares em caso de incidente de segurança; (vii) medidas técnicas e administrativas previstas no art. 46.

11.4. **Minimização em canais de suporte.** Ao abrir *issues*, enviar *bug reports* ou *logs* ao Licenciante, o Licenciado se compromete a **minimizar Dados Pessoais**: redigir/mascarar nomes, e-mails, IPs e demais identificadores antes de transmitir, salvo quando a informação for estritamente necessária para a reprodução do problema. O Licenciante poderá rejeitar e descartar *attachments* que contenham Dados Pessoais excessivos.

11.5. **Transferências internacionais.** Se o Licenciado optar por hospedar o Software em provedor de nuvem fora do Brasil, a transferência internacional de Dados Pessoais é de **responsabilidade exclusiva do Licenciado**, devendo este observar os arts. 33 a 36 da LGPD e adotar mecanismos legais cabíveis (cláusulas-padrão, certificação, etc.).

11.6. **Cooperação com a ANPD.** As partes cooperarão de boa-fé com a Autoridade Nacional de Proteção de Dados em eventuais procedimentos relacionados ao Software.

---

## 12. COMPONENTES DE SOFTWARE DE TERCEIROS (OPEN SOURCE)

12.1. O Software incorpora ou depende de Componentes de Terceiros, principalmente *open source*, listados de forma exaustiva nos arquivos ``backend/requirements.lock`` e ``frontend/pnpm-lock.yaml`` e arquivos análogos.

12.2. Cada Componente de Terceiros é regido por sua **licença original** (Apache 2.0, MIT, BSD, LGPL, MPL, etc., conforme o caso). Nada neste EULA restringe os direitos do Licenciado decorrentes dessas licenças com relação aos próprios Componentes de Terceiros.

12.3. O Licenciante **não fornece garantias** quanto aos Componentes de Terceiros, os quais são fornecidos "AS IS" pelos seus respectivos autores. Eventuais falhas ou vulnerabilidades em Componentes de Terceiros serão tratadas, na medida do possível, mediante atualização do Software conforme Seção 7.

12.4. **Cisão.** Caso uma licença de Componente de Terceiros entre em conflito com este EULA quanto ao próprio Componente, prevalecerá a licença original do Componente em relação a ele, sem que isso afete a aplicação deste EULA ao restante do Software.

---

## 13. CONFORMIDADE LEGAL E CONTROLE DE EXPORTAÇÃO

13.1. **Conformidade.** O Licenciado declara e garante que: (i) utilizará o Software em conformidade com todas as leis aplicáveis; (ii) não está localizado em país sujeito a sanções abrangentes pelos Estados Unidos, União Europeia, Reino Unido ou Brasil; (iii) não consta em listas de sanções (OFAC SDN, EU Consolidated, UK HMT, ONU); (iv) não fornecerá o Software, direta ou indiretamente, a pessoa ou entidade sancionada.

13.2. **Controle de exportação.** O CentralOps é classificável como produto dual-use (segurança da informação). O Licenciado declara e garante que **não exportará nem reexportará** o Software para destinos sujeitos a controle de exportação sem prévia obtenção das autorizações governamentais cabíveis.

13.3. **Crimes cibernéticos.** O Licenciado utilizará o Software exclusivamente em operações **defensivas e autorizadas**. É **expressamente vedado** o uso em qualquer atividade ofensiva não autorizada, exfiltração de dados de terceiros sem consentimento, acesso não autorizado a sistemas alheios ou qualquer conduta tipificada nas Leis nº 12.737/2012 e nº 14.155/2021.

---

## 14. AUDITORIA

14.1. **Direito de auditoria.** Durante a vigência deste EULA e por **2 (dois) anos** após sua extinção, o Licenciante poderá, mediante aviso prévio escrito de **15 (quinze) Dias Úteis**, auditar o uso que o Licenciado faz do Software, exclusivamente para verificar conformidade com as Seções 4, 5, 11 e 13.

14.2. **Escopo da auditoria.** A auditoria será conduzida em horário comercial, por auditor independente designado pelo Licenciante, sob obrigação de sigilo, sem prejuízo desproporcional às operações do Licenciado.

14.3. **Custos.** Os custos da auditoria correrão por conta do Licenciante. Se a auditoria identificar **violação material**, o Licenciado reembolsará os custos da auditoria, sem prejuízo das medidas indenizatórias cabíveis.

14.4. **Periodicidade.** Auditorias não ocorrerão com frequência superior a 1 (uma) por ano civil, salvo em caso de suspeita razoável e documentada de violação.

---

## 15. GARANTIAS E DECLARAÇÕES

15.1. **Garantia limitada de conformidade.** Pelo período de Validade Técnica, o Licenciante garante que o Software, quando operado em ambiente compatível e configurado conforme a Documentação, funcionará **substancialmente em conformidade** com suas especificações declaradas.

15.2. **Remédio exclusivo.** O remédio exclusivo do Licenciado em caso de violação da garantia da Seção 15.1 será, a critério do Licenciante: (i) correção do defeito; (ii) fornecimento de *workaround*; ou (iii) reembolso de valores eventualmente pagos pela versão defeituosa, limitado ao previsto na Seção 16.

15.3. **EXCLUSÃO DE OUTRAS GARANTIAS.** EXCETO PELA GARANTIA EXPRESSA NA SEÇÃO 15.1, O SOFTWARE É FORNECIDO **"COMO ESTÁ" ("AS IS") E "CONFORME DISPONÍVEL" ("AS AVAILABLE")**, SEM QUALQUER OUTRA GARANTIA, EXPRESSA OU IMPLÍCITA, INCLUINDO, MAS NÃO LIMITADO A, GARANTIAS DE COMERCIABILIDADE, ADEQUAÇÃO A UM PROPÓSITO ESPECÍFICO, NÃO VIOLAÇÃO DE DIREITOS DE TERCEIROS, AUSÊNCIA DE ERROS, PRECISÃO, INTEGRIDADE OU OPERAÇÃO ININTERRUPTA.

15.4. **Riscos específicos de plataforma SOC.** O Licenciado reconhece que o Software é ferramenta de apoio à operação de segurança e **não substitui** julgamento humano qualificado, planos de resposta a incidentes, análise forense profissional ou seguros de cibersegurança. Falsos positivos, falsos negativos e atrasos são inerentes ao gênero. O Licenciado dimensionará sua arquitetura de defesa de modo a não depender exclusivamente do Software.

15.5. **Limites legais.** Na medida em que normas imperativas (em especial o CDC, quando aplicável) vedem a exclusão de determinadas garantias, as cláusulas de exclusão serão lidas como limitadas ao máximo permitido por lei, preservando-se as demais.

---

## 16. LIMITAÇÃO DE RESPONSABILIDADE

16.1. **Exclusão de danos indiretos.** NA MÁXIMA EXTENSÃO PERMITIDA PELA LEI APLICÁVEL, EM NENHUMA HIPÓTESE O LICENCIANTE SERÁ RESPONSÁVEL POR QUALQUER DANO INDIRETO, INCIDENTAL, ESPECIAL, CONSEQUENCIAL, EXEMPLAR OU PUNITIVO, OU POR LUCROS CESSANTES, PERDA DE RECEITA, PERDA DE DADOS, PERDA DE OPORTUNIDADE COMERCIAL, INTERRUPÇÃO DE NEGÓCIO, DANO À IMAGEM OU À REPUTAÇÃO, AINDA QUE O LICENCIANTE TENHA SIDO ADVERTIDO DA POSSIBILIDADE DE TAIS DANOS.

16.2. **Limite agregado.** NA MÁXIMA EXTENSÃO PERMITIDA POR LEI, A RESPONSABILIDADE TOTAL AGREGADA DO LICENCIANTE PERANTE O LICENCIADO, INDEPENDENTEMENTE DA TEORIA JURÍDICA (CONTRATUAL, EXTRACONTRATUAL, OBJETIVA OU OUTRA), DECORRENTE OU RELACIONADA A ESTE EULA OU AO SOFTWARE, NÃO EXCEDERÁ O MAIOR VALOR ENTRE: (i) **R$ 1.000,00 (mil reais)**; ou (ii) o valor efetivamente pago pelo Licenciado ao Licenciante a título de licenciamento do Software nos 12 (doze) meses anteriores ao evento gerador da responsabilidade.

16.3. **Exclusões legais.** As limitações da Seção 16.1 e 16.2 **não se aplicam** aos casos em que a lei vede sua limitação, incluindo, sem limitação: (i) dolo ou culpa grave do Licenciante (art. 392 do Código Civil); (ii) violação intencional de direitos de propriedade intelectual; (iii) violação de normas imperativas de proteção de dados pessoais.

16.4. **Distribuição de risco.** O Licenciado reconhece que (i) o valor licenciado é gratuito ou simbólico em relação ao porte das operações suportadas pelo Software, (ii) as limitações desta Seção são parte essencial do equilíbrio econômico do EULA, e (iii) sem essas limitações, o Licenciante não disponibilizaria o Software nos termos atuais.

---

## 17. INDENIZAÇÃO

17.1. **Pelo Licenciado.** O Licenciado defenderá, indenizará e isentará o Licenciante e suas pessoas relacionadas de e contra quaisquer perdas, custos, despesas (incluindo honorários advocatícios razoáveis) decorrentes de: (i) violação deste EULA pelo Licenciado; (ii) uso ilícito do Software pelo Licenciado, seus empregados, contratados ou agentes; (iii) violação por parte do Licenciado de direitos de terceiros relacionados ao tratamento de Dados Pessoais; (iv) declarações falsas feitas pelo Licenciado neste EULA.

17.2. **Pelo Licenciante (escopo limitado).** Caso terceiro alegue que o uso do Software, dentro do escopo deste EULA, viole direito autoral ou marca registrada de titularidade brasileira, o Licenciante, a seu critério: (i) defenderá o Licenciado, suportando os custos diretos da defesa; (ii) substituirá ou modificará o Software para evitar a alegada violação; ou (iii) rescindirá esta licença e cessará o uso. A obrigação do Licenciante prevista nesta Seção 17.2 está limitada ao previsto na Seção 16.2.

17.3. **Exceções à indenização pelo Licenciante.** A obrigação da Seção 17.2 **não se aplica** se a alegação decorrer de: (i) modificação do Software pelo Licenciado; (ii) combinação do Software com outro produto não fornecido pelo Licenciante; (iii) uso fora do escopo autorizado deste EULA; (iv) uso após disponibilização de versão atualizada que resolveria a alegação.

17.4. **Procedimento.** O Licenciante deverá ser notificado em até **5 (cinco) Dias Úteis** sobre qualquer reivindicação coberta por esta Seção. A omissão na notificação dispensa o Licenciante de defender o Licenciado, na medida em que tal omissão tenha causado prejuízo material à defesa.

---

## 18. PRAZO E RESCISÃO

18.1. **Início da vigência.** Este EULA entra em vigor no momento da aceitação pelo Licenciado (Seção 2) e permanece em vigor enquanto o Licenciado utilizar o Software, salvo extinção antecipada.

18.2. **Rescisão pelo Licenciado.** O Licenciado pode rescindir este EULA, a qualquer tempo, mediante cessação completa do uso e remoção integral do Software de seus sistemas.

18.3. **Rescisão pelo Licenciante por descumprimento.** O Licenciante pode rescindir este EULA imediatamente, mediante notificação (que pode incluir aviso no repositório público), em caso de violação material pelo Licenciado, em particular das Seções 4, 5, 11 ou 13.

18.4. **Efeitos da rescisão.** Após a rescisão, o Licenciado deverá, no prazo de **30 (trinta) dias**: (i) cessar todo uso do Software; (ii) destruir todas as cópias do Software e Imagens em sua posse ou controle; (iii) certificar a destruição por escrito se solicitado pelo Licenciante.

18.5. **Cláusulas que sobrevivem.** Sobrevivem à rescisão, naquilo que sua natureza permitir: as Seções 3, 5, 6, 9, 10, 11, 13, 14, 15.3, 16, 17, 18.4, 22 e 23.

---

## 19. CASO FORTUITO E FORÇA MAIOR

19.1. Nenhuma parte será responsável por inadimplemento decorrente de eventos fora de seu controle razoável, incluindo, sem limitação: caso fortuito, força maior (art. 393 do Código Civil), guerra, terrorismo, pandemia, indisponibilidade prolongada de infraestrutura pública de telecomunicações ou energia, atos de autoridade pública, ataques cibernéticos massivos (DDoS, ransomware de larga escala atingindo terceiros essenciais), greves não previsíveis, *zero-day* explorado em Componente de Terceiros antes da disponibilidade pública do *patch*.

19.2. A parte afetada notificará a outra em prazo razoável e adotará esforços comercialmente razoáveis para mitigar e retomar a execução.

---

## 20. CESSÃO E SUCESSÃO

20.1. **Pelo Licenciado.** O Licenciado **não pode** ceder, transferir ou onerar este EULA, no todo ou em parte, sem o prévio consentimento escrito do Licenciante. Para evitar dúvidas, operações societárias que impliquem mudança de controle do Licenciado constituem cessão para os fins desta Seção 20.1.

20.2. **Pelo Licenciante.** O Licenciante pode ceder livremente este EULA, no todo ou em parte, mediante aviso ao Licenciado, em particular em casos de: (i) reorganização societária; (ii) alienação do acervo de propriedade intelectual; (iii) sucessão patrimonial.

20.3. Este EULA vincula sucessores legais autorizados.

---

## 21. COMUNICAÇÕES E NOTIFICAÇÕES

21.1. **Notificações ao Licenciante.** Devem ser remetidas, preferencialmente, pelo *issue tracker* do repositório público do Software, ou por e-mail indicado pelo Licenciante em canal oficial.

21.2. **Notificações ao Licenciado.** Serão consideradas validamente realizadas mediante: (i) postagem pelo Licenciante no repositório oficial do Software (``README`` ou ``CHANGELOG``); (ii) publicação na Documentação; ou (iii) envio para e-mail fornecido pelo Licenciado em interação prévia (suporte, *bug report*).

21.3. **Forma escrita.** "Por escrito" abrange comunicações eletrônicas, observados os padrões mínimos de autenticidade e integridade.

---

## 22. LEI APLICÁVEL E FORO

22.1. **Lei aplicável.** Este EULA é regido e interpretado exclusivamente pelas **Leis da República Federativa do Brasil**, com expressa exclusão de regras de conflito de leis.

22.2. **Não aplicação da CISG.** A Convenção das Nações Unidas sobre Contratos de Compra e Venda Internacional de Mercadorias (CISG / Convenção de Viena de 1980) **não se aplica** a este EULA.

22.3. **Tentativa de resolução amigável.** Antes de qualquer medida judicial, as partes envidarão esforços razoáveis para resolução consensual, por meio de notificação extrajudicial com prazo de resposta de **15 (quinze) dias** corridos.

22.4. **Foro de eleição.** Fica eleito o foro da **Comarca do domicílio do Licenciante**, no Estado da Federação onde este resida, com renúncia expressa a qualquer outro, por mais privilegiado que seja, para dirimir controvérsias oriundas deste EULA, ressalvadas: (i) hipóteses em que normas processuais imperativas (em especial as do CDC para consumidores pessoas físicas) determinem foro distinto; (ii) ações que envolvam questões registrárias.

22.5. **Arbitragem (opcional).** As partes podem, mediante acordo posterior por escrito, submeter controvérsias específicas à arbitragem perante câmara reconhecida (CAM/CCBC, AMCHAM ou equivalente), regida pela Lei nº 9.307/96.

---

## 23. DISPOSIÇÕES GERAIS

23.1. **Acordo integral.** Este EULA, em conjunto com seus Anexos, constitui o **acordo integral** entre Licenciante e Licenciado em relação à matéria nele tratada, prevalecendo sobre quaisquer entendimentos prévios, verbais ou escritos.

23.2. **Independência das cláusulas.** Caso qualquer cláusula deste EULA seja declarada nula, inválida ou inexequível, as demais permanecerão em pleno vigor, e a cláusula afetada será interpretada da forma que mais se aproxime de sua intenção original dentro dos limites legais.

23.3. **Não renúncia.** A tolerância de qualquer parte ao descumprimento de cláusula deste EULA não implica renúncia ao direito de exigir o cumprimento futuro nem novação contratual.

23.4. **Modificação.** O Licenciante pode atualizar este EULA periodicamente. A versão atualizada será publicada com nova "Data de vigência" no cabeçalho. O uso continuado do Software após a Data de vigência da nova versão constitui aceitação dos novos termos. Caso o Licenciado não concorde, deverá rescindir conforme Seção 18.2.

23.5. **Idiomas.** Em caso de conflito entre versões em diferentes idiomas, prevalecerá a versão em **português brasileiro**, originalmente redigida.

23.6. **Boa-fé.** As partes executarão este EULA de acordo com os princípios de probidade e boa-fé (arts. 113, 187 e 422 do Código Civil).

23.7. **Independência das partes.** Nada neste EULA cria relação societária, *joint venture*, sociedade de fato, representação, mandato ou vínculo empregatício entre as partes.

---

## 24. GLOSSÁRIO RÁPIDO

| Termo | Significado resumido |
|---|---|
| Software | A plataforma CentralOps e tudo que a compõe |
| Licenciante | Dathan Vitor Santana da Nobrega |
| Licenciado | Quem usa o Software após aceitar este EULA |
| Imagem | Container Docker oficial do Software |
| Código-Fonte | Arquivos legíveis (.py, .ts, .yml etc.) |
| Artefatos Compilados | .so, bundle JS, bytecode |
| Uso Interno | Operações de segurança próprias do Licenciado |
| Serviço Comercial | Oferta a terceiros (SaaS, MDR, SOCaaS); requer contrato separado |
| Engenharia Reversa | Tentativa de obter Código-Fonte a partir do binário |
| Trabalho Derivado | Qualquer obra baseada no Software |
| Validade Técnica | Período mínimo de suporte de uma versão (12 meses) |
| LGPD | Lei nº 13.709/2018 |
| Lei do Software | Lei nº 9.609/98 |

---

## ANEXO I — TERMOS ESPECÍFICOS PARA IMAGEM DOCKER PÚBLICA

A.I.1. **Conveniência operacional.** A disponibilização da Imagem em registry público é mera conveniência. **A publicidade da Imagem não converte o Software em open source**, nem altera a natureza proprietária declarada na Seção 1.2.

A.I.2. **Camada de proteção.** Os módulos com maior valor de propriedade intelectual (engine de coleta, providers, services, routers, core, api, utils) são distribuídos como artefatos Cython-compilados (``.so``), sem o respectivo Código-Fonte original. Qualquer tentativa de reconstruir esse Código-Fonte constitui Engenharia Reversa vedada pela Seção 5.1.

A.I.3. **Vedação a republicação.** O Licenciado não republicará a Imagem em registry de terceiros, *mirror*, repositório próprio ou similar.

A.I.4. **Marcações no manifest.** Eventuais *labels* OCI/Docker no manifest da Imagem que façam referência ao Licenciante e a este EULA não podem ser removidas ou alteradas.

A.I.5. **Telemetria.** O Licenciante pode (mas não é obrigado) coletar telemetria mínima e anônima sobre a Imagem (por exemplo: contagem de *pulls* via *registry analytics*) para fins de planejamento técnico, sem coletar Dados Pessoais.

A.I.6. **Disponibilidade.** A Imagem é fornecida "AS IS" no registry, podendo ser removida, depreciada ou modificada a qualquer tempo pelo Licenciante, observado o aviso de descontinuidade da Seção 7.4.

---

## ANEXO II — TRATAMENTO DE DADOS PESSOAIS (DPA SIMPLIFICADO)

Aplicável às situações da Seção 11.2(b), em que o Licenciante atua como Operador de Dados Pessoais que o Licenciado eventualmente transmita ao Licenciante (suporte, *bug reports*, *crash dumps*).

A.II.1. **Objeto.** Tratamento de Dados Pessoais incidentalmente recebidos pelo Licenciante em razão da prestação de suporte técnico.

A.II.2. **Finalidade.** Exclusivamente: (i) reprodução e diagnóstico do problema reportado; (ii) desenvolvimento de correção; (iii) verificação do *fix*.

A.II.3. **Categorias de dados.** Aquelas que o Licenciado, sob sua exclusiva responsabilidade, transmitir. O Licenciante recomenda a **prévia minimização e anonimização**, especialmente para dados sensíveis (LGPD, art. 5º, II).

A.II.4. **Categorias de titulares.** Aquelas decorrentes do contexto do Licenciado (funcionários, clientes, usuários finais — sob responsabilidade do Licenciado como Controlador).

A.II.5. **Subcontratação.** O Licenciante não subcontratará o Tratamento a terceiros sem aviso ao Licenciado. Eventuais Operadores secundários ficarão sujeitos a obrigações equivalentes.

A.II.6. **Segurança.** O Licenciante adotará medidas técnicas e organizacionais razoáveis para proteger os Dados Pessoais recebidos, incluindo controle de acesso restrito, eliminação após resolução do incidente e armazenamento criptografado quando aplicável.

A.II.7. **Prazo de retenção.** Dados Pessoais incidentalmente recebidos serão eliminados em até **90 (noventa) dias** após o encerramento do *issue* ou do *bug report*, salvo necessidade legal de retenção.

A.II.8. **Auxílio ao Controlador.** O Licenciante auxiliará o Licenciado, na medida razoável, no atendimento a solicitações de titulares e a determinações da ANPD.

A.II.9. **Incidentes.** Em caso de incidente de segurança envolvendo Dados Pessoais transmitidos pelo Licenciado ao Licenciante, o Licenciante notificará o Licenciado em até **48 (quarenta e oito) horas** úteis após a constatação.

A.II.10. **Devolução/eliminação.** Encerrado este EULA ou a finalidade específica do Tratamento, o Licenciante eliminará os Dados Pessoais, salvo obrigação legal de retenção.

---

## ASSINATURA / ACEITAÇÃO ELETRÔNICA

**Pelo Licenciante:**
Dathan Vitor Santana da Nobrega
*Assinatura constante no commit que introduz esta versão deste EULA no repositório oficial do Software.*

**Pelo Licenciado:**
*Aceitação eletrônica realizada nos termos da Seção 2 deste EULA, mediante uso do Software.*

---

**Versão consolidada:** 2.0 — 14 de maio de 2026
**Próxima revisão recomendada:** 14 de maio de 2027 (ou antes, em caso de alteração legislativa material ou mudança no modelo de distribuição).

---

© 2026 Dathan Vitor Santana da Nobrega. Todos os direitos reservados.
