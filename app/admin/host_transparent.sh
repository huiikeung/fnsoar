#!/bin/bash
# fnSoar host-transparent mode: IPv4/IPv6 TCP + DNS interception.
# IPv6 UDP/QUIC is fail-closed here; use TUN for NAS-local IPv6 UDP.
set -eu
ACTION="${1:-apply}"
TABLE="fnsoar_host"
V6_TABLE="fnsoar_host_v6"
V6_FILTER="fnsoar_host_v6_filter"
V6_TPROXY_LEGACY="fnsoar_host_v6_tproxy"
UID_NAME="${FNSOAR_UID_NAME:-fnsoar}"
PORT="${FNSOAR_REDIR_PORT:-7892}"
DNS_PORT=1053
NFT=/usr/sbin/nft
IPT=/usr/sbin/iptables
cleanup_legacy() {
  "$IPT" -t nat -D OUTPUT -j FNSOAR_HOST_REDIRECT 2>/dev/null || true
  "$IPT" -t nat -D OUTPUT -p udp --dport 53 -j FNSOAR_HOST_DNS 2>/dev/null || true
  "$IPT" -t nat -F FNSOAR_HOST_REDIRECT 2>/dev/null || true
  "$IPT" -t nat -F FNSOAR_HOST_DNS 2>/dev/null || true
  "$IPT" -t nat -X FNSOAR_HOST_REDIRECT 2>/dev/null || true
  "$IPT" -t nat -X FNSOAR_HOST_DNS 2>/dev/null || true
}
cleanup() {
  "$NFT" delete table ip "$TABLE" 2>/dev/null || true
  "$NFT" delete table ip6 "$V6_TABLE" 2>/dev/null || true
  "$NFT" delete table ip6 "$V6_FILTER" 2>/dev/null || true
  "$NFT" delete table ip6 "$V6_TPROXY_LEGACY" 2>/dev/null || true
  cleanup_legacy
}
if [ "$ACTION" = cleanup ]; then cleanup; exit 0; fi
[ "$ACTION" = apply ] || { echo "usage: $0 {apply|cleanup}" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "需要 root 权限" >&2; exit 1; }
[ -x "$NFT" ] || { echo "系统没有 nft" >&2; exit 1; }
id "$UID_NAME" >/dev/null 2>&1 || { echo "专用用户不存在: $UID_NAME" >&2; exit 1; }
cleanup
"$NFT" -f - <<EOF
 table ip $TABLE {
   chain output {
     type nat hook output priority -100; policy accept;
     meta skuid $UID_NAME return
     ip daddr { 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 240.0.0.0/4 } return
     tcp dport { 7890, 7892, 7893, 9090, 9099, 1053 } return
     udp dport 53 redirect to :$DNS_PORT
     udp dport 443 drop
     tcp dport != 0 redirect to :$PORT
   }
 }
 table ip6 $V6_TABLE {
   chain output {
     type nat hook output priority -100; policy accept;
     meta skuid $UID_NAME return
     ip6 daddr { ::1/128, fc00::/7, fe80::/10, ff00::/8 } return
     tcp dport { 7890, 7892, 7893, 9090, 9099, 1053 } return
     udp dport 53 redirect to :$DNS_PORT
     tcp dport != 0 redirect to :$PORT
   }
 }
 table ip6 ${V6_TABLE}_filter {
   chain output {
     type filter hook output priority 0; policy accept;
     meta skuid $UID_NAME return
     ip6 daddr { ::1/128, fc00::/7, fe80::/10, ff00::/8 } return
     meta l4proto udp drop
   }
 }
EOF
