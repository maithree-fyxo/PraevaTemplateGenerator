import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

export interface PreviewCounts {
  engaged_profiles: number;
  pipeline_table: number;
  discounted_profiles: number;
  discounted_table: number;
  target: string;
}
export interface Preview {
  mock_mode?: boolean;
  assignment: string;
  title: string;
  date: string;
  counts: PreviewCounts;
}
export interface ConfigStatus {
  token_configured: boolean;
  token_hint: string;
  token_source: string;
  mock_mode: boolean;
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  constructor(private http: HttpClient) {}

  getConfig() {
    return firstValueFrom(this.http.get<ConfigStatus>('/api/config'));
  }
  saveToken(token: string) {
    return firstValueFrom(
      this.http.post<ConfigStatus>('/api/config/token', { token })
    );
  }
  clearToken() {
    return firstValueFrom(this.http.delete<ConfigStatus>('/api/config/token'));
  }
  testToken() {
    return firstValueFrom(
      this.http.post<{ ok: boolean; status: number | null; detail: string }>(
        '/api/config/test',
        {}
      )
    );
  }

  previewUrl(url: string) {
    return firstValueFrom(
      this.http.get<Preview>('/api/preview', { params: { url } })
    );
  }
  diagnose(url: string) {
    return firstValueFrom(this.http.get<any>('/api/diagnose', { params: { url } }));
  }

  previewExcel(file: File) {
    const fd = new FormData();
    fd.append('file', file);
    return firstValueFrom(this.http.post<Preview>('/api/preview-from-excel', fd));
  }

  /** POST that returns a .pptx blob + filename from Content-Disposition. */
  async downloadPptx(path: string, body: BodyInit, isForm: boolean): Promise<void> {
    const res = await fetch(path, {
      method: 'POST',
      headers: isForm ? undefined : { 'Content-Type': 'application/json' },
      body,
    });
    if (!res.ok) {
      let detail = 'Generation failed';
      try {
        detail = (await res.json()).detail || detail;
      } catch {}
      throw new Error(detail);
    }
    const blob = await res.blob();
    const cd = res.headers.get('Content-Disposition') || '';
    let name = 'Praeva Search Update.pptx';
    const star = cd.match(/filename\*=(?:UTF-8'')?([^;]+)/i);
    const plain =
      cd.match(/filename="([^"]+)"/i) || cd.match(/filename=([^;]+)/i);
    if (star) {
      try {
        name = decodeURIComponent(star[1].trim().replace(/"/g, ''));
      } catch {
        name = star[1];
      }
    } else if (plain) {
      name = plain[1].trim();
    }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }
}
