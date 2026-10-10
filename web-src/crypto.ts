// Credential sealing, ported verbatim from the frozen setup.js. The browser encrypts
// the secret for the TARGET DEVICE's public key (RSA-OAEP wrapping an AES-GCM key);
// the plaintext secret never reaches the coordinator or the model. Security-critical:
// do not alter the algorithm/parameters.
const from64 = (value: string) => Uint8Array.from(atob(value), (c) => c.charCodeAt(0));
const to64 = (value: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(value)));

export interface SecretTarget {public_key: string; context: string; host_id?: string; installation_id?: string; endpoint?: string; storage?: string}

export async function sealCredential(value: string, target: SecretTarget) {
  const key = await crypto.subtle.importKey('spki', from64(target.public_key), {name: 'RSA-OAEP', hash: 'SHA-256'}, false, ['encrypt']);
  const aes = await crypto.subtle.generateKey({name: 'AES-GCM', length: 256}, true, ['encrypt']);
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    {name: 'AES-GCM', iv: nonce, additionalData: new TextEncoder().encode(target.context)},
    aes, new TextEncoder().encode(value));
  const wrapped = await crypto.subtle.encrypt({name: 'RSA-OAEP'}, key, await crypto.subtle.exportKey('raw', aes));
  return {key: to64(wrapped), nonce: to64(nonce.buffer), ciphertext: to64(ciphertext)};
}
