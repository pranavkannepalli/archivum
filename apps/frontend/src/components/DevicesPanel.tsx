import type { McpDevice, PairingToken } from '../api';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';

type DevicesPanelProps = {
  devices: McpDevice[];
  pairing: PairingToken | null;
  legacyKeyConfigured: boolean;
  onLink: () => void;
  onRevoke: (deviceId: string) => void;
};

function formatDate(iso: string | null) {
  if (!iso) return 'Never';
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  } catch {
    return iso;
  }
}

export function DevicesPanel({ devices, pairing, legacyKeyConfigured, onLink, onRevoke }: DevicesPanelProps) {
  return (
    <div className="space-y-3">
      <Button type="button" variant="outline" size="sm" onClick={onLink}>
        Link a device
      </Button>

      {pairing && (
        <div className="soft-border rounded-[8px] border bg-white/[0.03] p-3">
          <p className="text-xs font-semibold uppercase tracking-[0.14em] text-zinc-500">
            Run this on the new machine
          </p>
          <code className="mt-2 block break-all font-mono text-xs text-muted-foreground">
            {`npx archivum@latest connect ${pairing.token}`}
          </code>
          <p className="mt-2 text-xs leading-5 text-muted-foreground">
            This token works once and expires in 15 minutes.
          </p>
        </div>
      )}

      {legacyKeyConfigured && (
        <div className="subtle-divider flex items-center justify-between gap-3 border-b py-2">
          <div className="min-w-0">
            <p className="text-sm text-foreground">legacy shared key</p>
            <p className="text-xs leading-5 text-muted-foreground">
              Authenticates every client that holds it. Retire it once machines are linked individually.
            </p>
          </div>
          <Badge variant="secondary" className="shrink-0 text-xs">
            Shared
          </Badge>
        </div>
      )}

      {devices.length === 0 ? (
        <p className="text-sm text-muted-foreground">No devices linked yet.</p>
      ) : (
        <div className="space-y-2">
          {devices.map((device) => (
            <div
              key={device.id}
              className="subtle-divider flex items-center justify-between gap-3 border-b py-2 last:border-0"
            >
              <div className="min-w-0">
                <p className="truncate text-sm text-foreground">{device.name}</p>
                <p className="truncate font-mono text-xs text-muted-foreground">{device.id}</p>
                <p className="text-xs text-muted-foreground">
                  Linked {formatDate(device.created_at)} · Last seen {formatDate(device.last_seen_at)}
                </p>
              </div>
              {device.revoked_at ? (
                <Badge variant="destructive" className="shrink-0 text-xs">
                  Revoked
                </Badge>
              ) : (
                <Button type="button" variant="outline" size="sm" onClick={() => onRevoke(device.id)}>
                  Revoke
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
