import { readFile } from 'node:fs/promises';

export const dynamic = 'force-dynamic';

export async function GET() {
  const path = process.env.PVE_LENS_SNAPSHOT || '/data/snapshot.json';
  try {
    const body = await readFile(path, 'utf8');
    return new Response(body, {
      headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
    });
  } catch {
    return Response.json({ error: 'snapshot unavailable' }, { status: 503 });
  }
}
