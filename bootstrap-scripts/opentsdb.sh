#!/bin/bash -v

set -e

cat > /etc/salt/grains <<EOF
cloudera:
  cluster_flavour: $PNDA_FLAVOR
pnda_cluster: $PNDA_CLUSTER
roles:
  - opentsdb
EOF
if [ $1 = 1 ]; then
cat >> /etc/salt/grains <<EOF
  - grafana
EOF
fi

cat >> /etc/salt/minion <<EOF
id: $PNDA_CLUSTER-opentsdb-$1
EOF

echo $PNDA_CLUSTER-opentsdb-$1 > /etc/hostname
hostname $PNDA_CLUSTER-opentsdb-$1

service salt-minion restart
