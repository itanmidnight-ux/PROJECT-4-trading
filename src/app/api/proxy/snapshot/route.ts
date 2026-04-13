import { apiError } from '@/lib/api-response';

export async function GET() {
  return apiError(
    'NOT_FOUND',
    'Legacy proxy endpoint disabled. Use /api/snapshot.',
    410
  );
}
