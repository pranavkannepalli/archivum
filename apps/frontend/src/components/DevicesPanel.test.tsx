import { describe, expect, it } from 'vitest';
import { renderToString } from 'react-dom/server';
import { DevicesPanel } from './DevicesPanel';

const device = {
  id: 'dev_abc',
  name: 'work laptop / claude',
  created_at: '2026-08-26T10:00:00Z',
  last_seen_at: '2026-08-26T11:00:00Z',
  revoked_at: null,
};

describe('DevicesPanel', () => {
  it('lists linked devices with when they were last seen', () => {
    const html = renderToString(
      <DevicesPanel devices={[device]} pairing={null} legacyKeyConfigured={false} onLink={() => {}} onRevoke={() => {}} />,
    );

    expect(html).toContain('work laptop / claude');
    expect(html).toContain('dev_abc');
  });

  it('shows a revoked device as revoked rather than hiding it', () => {
    const html = renderToString(
      <DevicesPanel
        devices={[{ ...device, revoked_at: '2026-08-26T12:00:00Z' }]}
        pairing={null}
        legacyKeyConfigured={false}
        onLink={() => {}}
        onRevoke={() => {}}
      />,
    );

    expect(html).toContain('Revoked');
    expect(html).toContain('work laptop / claude');
  });

  it('shows a command that works today rather than an unpublished npx package', () => {
    const html = renderToString(
      <DevicesPanel
        devices={[]}
        pairing={{ token: 'arch1_xyz', expires_at: '2026-08-26T10:15:00Z' }}
        legacyKeyConfigured={false}
        onLink={() => {}}
        onRevoke={() => {}}
      />,
    );

    expect(html).toContain('git clone https://github.com/pranavkannepalli/archivum.git');
    expect(html).toContain('node packages/archivum-cli/src/index.js connect arch1_xyz');
    // The npx form is named only as what this becomes once published; it must
    // never be the line the user is told to run against a live token.
    expect(html).not.toContain('npx archivum@latest connect arch1_xyz');
    expect(html).toContain('once the CLI is on public npm');
    expect(html).toContain('15 minutes');
  });

  it('offers a copy button for the pairing token', () => {
    const html = renderToString(
      <DevicesPanel
        devices={[]}
        pairing={{ token: 'arch1_xyz', expires_at: '2026-08-26T10:15:00Z' }}
        legacyKeyConfigured={false}
        onLink={() => {}}
        onRevoke={() => {}}
        onCopyToken={() => {}}
      />,
    );

    expect(html).toContain('Copy token');
  });

  it('reports a copied token back to the user', () => {
    const html = renderToString(
      <DevicesPanel
        devices={[]}
        pairing={{ token: 'arch1_xyz', expires_at: '2026-08-26T10:15:00Z' }}
        legacyKeyConfigured={false}
        tokenCopied
        onLink={() => {}}
        onRevoke={() => {}}
        onCopyToken={() => {}}
      />,
    );

    expect(html).toContain('Copied token');
  });

  it('offers a way to dismiss a spent pairing token', () => {
    const html = renderToString(
      <DevicesPanel
        devices={[]}
        pairing={{ token: 'arch1_xyz', expires_at: '2026-08-26T10:15:00Z' }}
        legacyKeyConfigured={false}
        onLink={() => {}}
        onRevoke={() => {}}
        onDismissPairing={() => {}}
      />,
    );

    expect(html).toContain('refresh devices');
  });

  it('names the legacy shared key and states how it is actually retired', () => {
    const html = renderToString(
      <DevicesPanel devices={[]} pairing={null} legacyKeyConfigured onLink={() => {}} onRevoke={() => {}} />,
    );

    expect(html).toContain('legacy shared key');
    expect(html).toContain('every client');
    // The shared key lives in .env, not in the device table, so the row must
    // say what retiring it takes rather than implying a revoke button exists.
    expect(html).toContain('MCP_API_KEY');
    expect(html).toContain('restarting');
  });

  it('says nothing about a legacy key when none is configured', () => {
    const html = renderToString(
      <DevicesPanel devices={[]} pairing={null} legacyKeyConfigured={false} onLink={() => {}} onRevoke={() => {}} />,
    );

    expect(html).not.toContain('legacy shared key');
  });
});
