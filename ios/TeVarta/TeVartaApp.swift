import AuthenticationServices
import Security
import SwiftUI

private struct AuthResponse: Decodable {
    let accessToken: String
    let refreshToken: String
    let tokenType: String
    let expiresAt: Date
    let user: User
}

private struct User: Codable {
    let id: String
    let timezone: String
}

private struct StoredSession: Codable {
    let accessToken: String
    let refreshToken: String
    let expiresAt: Date
    let user: User

    init(response: AuthResponse) {
        accessToken = response.accessToken
        refreshToken = response.refreshToken
        expiresAt = response.expiresAt
        user = response.user
    }
}

private enum AuthenticationError: LocalizedError {
    case configuration
    case missingIdentityToken
    case invalidResponse
    case server(String)

    var errorDescription: String? {
        switch self {
        case .configuration:
            return "The API endpoint has not been configured."
        case .missingIdentityToken:
            return "Apple did not return an identity token. Please try again."
        case .invalidResponse:
            return "The server returned an invalid authentication response."
        case let .server(message):
            return message
        }
    }
}

private struct AuthAPIClient {
    private let baseURL: URL
    private let session: URLSession
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    init(bundle: Bundle = .main, session: URLSession = .shared) throws {
        guard let value = bundle.object(forInfoDictionaryKey: "TEVARTA_API_BASE_URL") as? String,
              let baseURL = URL(string: value),
              baseURL.scheme == "https"
        else { throw AuthenticationError.configuration }
        self.baseURL = baseURL
        self.session = session
        decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .iso8601
        encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
    }

    func signIn(identityToken: String, authorizationCode: String?) async throws -> AuthResponse {
        try await request(
            path: "v1/auth/apple",
            body: ["identityToken": identityToken, "authorizationCode": authorizationCode]
        )
    }

    func refresh(refreshToken: String) async throws -> AuthResponse {
        try await request(path: "v1/auth/refresh", body: ["refreshToken": refreshToken])
    }

    func signOut(accessToken: String) async throws {
        var request = URLRequest(url: baseURL.appending(path: "v1/auth/sign-out"))
        request.httpMethod = "POST"
        request.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization")
        let (_, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse, response.statusCode == 204 else {
            throw AuthenticationError.server("Sign-out could not be completed.")
        }
    }

    private func request<T: Decodable>(path: String, body: [String: String?]) async throws -> T {
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try encoder.encode(body)
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw AuthenticationError.invalidResponse }
        guard (200 ..< 300).contains(response.statusCode) else {
            throw AuthenticationError.server("Authentication failed. Please try again.")
        }
        do { return try decoder.decode(T.self, from: data) }
        catch { throw AuthenticationError.invalidResponse }
    }
}

private enum KeychainSessionStore {
    private static let service = "com.tevarta.beta.auth"
    private static let account = "refresh-session"

    static func save(_ session: StoredSession) throws {
        let data = try JSONEncoder().encode(session)
        let item = baseQuery().merging([kSecValueData as String: data]) { _, new in new }
        SecItemDelete(baseQuery() as CFDictionary)
        guard SecItemAdd(item as CFDictionary, nil) == errSecSuccess else {
            throw AuthenticationError.server("The session could not be stored securely.")
        }
    }

    static func load() throws -> StoredSession? {
        let query = baseQuery().merging([kSecReturnData as String: true]) { _, new in new }
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else {
            throw AuthenticationError.server("The stored session could not be read.")
        }
        return try JSONDecoder().decode(StoredSession.self, from: data)
    }

    static func clear() { SecItemDelete(baseQuery() as CFDictionary) }

    private static func baseQuery() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
    }
}

@MainActor
private final class AuthenticationViewModel: ObservableObject {
    @Published private(set) var session: StoredSession?
    @Published var errorMessage: String?
    @Published private(set) var isWorking = false

    init() { session = try? KeychainSessionStore.load() }

    func signIn(credential: ASAuthorizationAppleIDCredential) async {
        guard let data = credential.identityToken,
              let identityToken = String(data: data, encoding: .utf8)
        else { errorMessage = AuthenticationError.missingIdentityToken.localizedDescription; return }
        let authorizationCode = credential.authorizationCode.flatMap { String(data: $0, encoding: .utf8) }
        await updateSession { try await AuthAPIClient().signIn(identityToken: identityToken, authorizationCode: authorizationCode) }
    }

    func refreshIfNeeded() async {
        guard let session, session.expiresAt <= .now else { return }
        await updateSession { try await AuthAPIClient().refresh(refreshToken: session.refreshToken) }
    }

    func signOut() async {
        guard let session else { return }
        isWorking = true
        defer { isWorking = false }
        do { try await AuthAPIClient().signOut(accessToken: session.accessToken) }
        catch { errorMessage = error.localizedDescription; return }
        KeychainSessionStore.clear()
        self.session = nil
    }

    private func updateSession(_ operation: () async throws -> AuthResponse) async {
        isWorking = true
        defer { isWorking = false }
        do {
            let updated = StoredSession(response: try await operation())
            try KeychainSessionStore.save(updated)
            session = updated
            errorMessage = nil
        } catch { errorMessage = error.localizedDescription }
    }
}

@main
struct TeVartaApp: App {
    @StateObject private var authentication = AuthenticationViewModel()

    var body: some Scene {
        WindowGroup {
            NavigationStack {
                ContentView(authentication: authentication)
            }
            .task { await authentication.refreshIfNeeded() }
        }
    }
}

private struct ContentView: View {
    @ObservedObject var authentication: AuthenticationViewModel

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "newspaper")
                .font(.system(size: 44))
                .foregroundStyle(Color.accentColor)
            Text("Te Varta")
                .font(.largeTitle.bold())
            if let user = authentication.session?.user {
                Text("Signed in as \(user.id)")
                    .foregroundStyle(.secondary)
                Button("Sign out", role: .destructive) {
                    Task { await authentication.signOut() }
                }
                .disabled(authentication.isWorking)
            } else {
                SignInWithAppleButton(.signIn) { request in
                    request.requestedScopes = []
                } onCompletion: { result in
                    guard case let .success(authorization) = result,
                          let credential = authorization.credential as? ASAuthorizationAppleIDCredential
                    else {
                        authentication.errorMessage = "Apple sign-in was cancelled or failed."
                        return
                    }
                    Task { await authentication.signIn(credential: credential) }
                }
                .signInWithAppleButtonStyle(.black)
                .frame(height: 50)
                .disabled(authentication.isWorking)
            }
            if let errorMessage = authentication.errorMessage {
                Text(errorMessage)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
            }
        }
        .padding()
        .navigationTitle("Your world")
    }
}
