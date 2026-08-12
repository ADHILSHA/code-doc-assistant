/** Known TypeScript module used by retrieval tests. */

export interface User {
  id: number;
  email: string;
}

export class UserClient {
  constructor(private readonly baseUrl: string) {}

  async getUserById(userId: number): Promise<User> {
    const res = await fetch(`${this.baseUrl}/users/${userId}`);
    if (!res.ok) {
      throw new Error(`failed to fetch user ${userId}: ${res.status}`);
    }
    return (await res.json()) as User;
  }

  async createUser(email: string, password: string): Promise<User> {
    const res = await fetch(`${this.baseUrl}/users`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    return (await res.json()) as User;
  }
}
