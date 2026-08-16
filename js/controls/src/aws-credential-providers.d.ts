// Minimal ambient declaration for the optional peer dependency, imported
// lazily at runtime. Consumers with the real package installed get its own
// richer types.
declare module "@aws-sdk/credential-providers" {
  export function fromNodeProviderChain(): unknown;
}
