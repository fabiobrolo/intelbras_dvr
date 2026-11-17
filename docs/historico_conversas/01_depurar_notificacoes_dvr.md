# Conversa 01: Depurar Notificações DVR Intelbras

- **Data de Ocorrência:** 14/09/2026
- **ID da Conversa:** [`46ef5d3e-03a3-4898-ac96-f1023e55f52c`](conversation://46ef5d3e-03a3-4898-ac96-f1023e55f52c)
- **Título Original:** Depurar Notificações DVR Intelbras
- **Quantidade de Passos:** 105

---

## 1. Contexto e Problema Reportado

O usuário reportou receber frequentemente em seu celular alertas indesejados da automação **"Componente Importante Parado"** com a mensagem:
> *"O DVR intelbras status está parado"*

No entanto, o DVR físico (`Intelbras iMHDX 3008`, IP `192.168.0.99`) continuava ligado e operando normalmente na rede local.

---

## 2. Diagnóstico Realizado

1. **Investigação da Entidade e Automação:**
   - Foi localizada a entidade de monitoramento: `sensor.intelbras_imhdx3008_192_168_0_99_status`.
   - A automação `Componente Importante Parado` monitorava múltiplos serviços vitais (Frigate, Zigbee2MQTT, AppDaemon, etc.).
   - O gatilho para o DVR estava configurado para disparar se o estado ficasse em `Offline` ou `unavailable` por apenas **2 minutos**.

2. **Causa Raiz Identificada:**
   - A integração `intelbras_dvr` realizava comunicação HTTP por polling com o DVR.
   - Em momentos de alto tráfego de vídeo na rede local ou durante manutenções internas do DVR, o dispositivo demorava para responder às requisições HTTP do Home Assistant, fazendo com que o HA marcasse temporariamente a entidade como `unavailable` por cerca de 3 a 5 minutos.
   - Como a janela de tolerância da automação era muito estreita (2 minutos), geravam-se falsos positivos de indisponibilidade.

---

## 3. Resolução Adotada

- Ajuste da automação no Home Assistant: elevação do tempo de tolerância contínua (`for:`) para **15 minutos** antes de emitir a notificação crítica no smartphone.
- Com isso, oscilações pontuais de rede ou reinicializações preventivas do DVR deixaram de acionar alertas falsos.
