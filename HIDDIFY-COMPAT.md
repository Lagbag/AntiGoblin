# AntiGoblin — Hiddify / multiprotocol compatibility

Этот форк использует два runtime-пути, не меняя существующую Keenetic/XKeen маршрутизацию:

- **Xray-native:** VLESS и VMess из URI (включая VLESS XHTTP) остаются на Xray.
- **sing-box bridge:** Hysteria2/HY2, Hysteria v1, Trojan, Shadowsocks, TUIC, AnyTLS, SOCKS, HTTP(S) CONNECT, SSH, Naive и raw sing-box/Hiddify nodes идут через локальный mixed SOCKS bridge `127.0.0.1:61225`.
- **UDP:** transparent UDP остаётся на sing-box TPROXY `:61221` и использует тот же активный upstream.

## Импорт

Поддерживаются URI:

`vless://`, `vmess://`, `hysteria2://`, `hy2://`, `hysteria://`, `trojan://`, `ss://`, `tuic://`, `anytls://`, `socks://`, `socks4://`, `socks4a://`, `socks5://`, `http://`, `https://`, `ssh://`, `naive+https://`, `naive+quic://`.

Также принимается raw sing-box/Hiddify JSON:

- standalone `outbounds[]` с `server` + `server_port` сохраняются без пересборки provider-specific полей;
- standalone `endpoints[]` с `peers[0].address/port` сохраняются без пересборки; современный WireGuard (sing-box 1.13+) поддержан именно этим путём;
- custom direct `detour` нормализуется в локальный `direct`;
- связанный `detour`, которому нужен другой proxy node, не обрезается молча — импорт помечается ошибкой.

За счёт raw passthrough могут работать Hiddify-specific standalone outbounds (например XHTTP/Mieru/ShadowTLS), если установленный `hiddify-sing-box` понимает их схему.

## Core

Installer предпочитает `hiddify-sing-box 1.13.0.h5` на Linux arm64/amd64. Если Hiddify asset недоступен или архитектура не поддерживается, используется совместимая ветка upstream `sing-box 1.13.21`.

NaiveProxy: если archive содержит `libcronet.so`, installer кладёт библиотеку рядом с бинарём (`/opt/sbin`) и в `/opt/lib`.

## Safety / apply

Перед заменой `/opt/etc/sing-box/xkeen.json` backend выполняет `sing-box check -c <temp-config>`. Невалидный импорт не должен заменять последний рабочий sing-box config.

Health/self-heal определяет реальный upstream процесс (`xray` или `sing-box`) и умеет читать адрес как из `outbounds[]`, так и из WireGuard `endpoints[]`.

## Ограничение

Это не копия Hiddify GUI/config-converter. Полные dependency-графы (`selector`, `urltest`, multi-hop proxy `detour`, top-level provider DNS/rules/services) не flatten'ятся в один AntiGoblin key. Для таких профилей нужен отдельный graph-preserving importer; текущая версия вместо повреждённого «полупрофиля» возвращает ошибку для зависимого узла.
