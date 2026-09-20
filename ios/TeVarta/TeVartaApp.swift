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

private struct AppleSignInRequest: Encodable {
    let identityToken: String
    let authorizationCode: String?
}

private struct RefreshRequest: Encodable {
    let refreshToken: String
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
    case unauthorized
    case server(String)

    var errorDescription: String? {
        switch self {
        case .configuration:
            return "The API endpoint has not been configured."
        case .missingIdentityToken:
            return "Apple did not return an identity token. Please try again."
        case .invalidResponse:
            return "The server returned an invalid authentication response."
        case .unauthorized:
            return "Your session has expired. Please sign in again."
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
            body: AppleSignInRequest(
                identityToken: identityToken, authorizationCode: authorizationCode
            )
        )
    }

    func refresh(refreshToken: String) async throws -> AuthResponse {
        try await request(path: "v1/auth/refresh", body: RefreshRequest(refreshToken: refreshToken))
    }

    func signOut(accessToken: String) async throws {
        var request = URLRequest(url: baseURL.appending(path: "v1/auth/sign-out"))
        request.httpMethod = "POST"
        request.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization")
        let (_, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else {
            throw AuthenticationError.server("Sign-out could not be completed.")
        }
        if response.statusCode == 401 { throw AuthenticationError.unauthorized }
        guard response.statusCode == 204 else {
            throw AuthenticationError.server("Sign-out could not be completed.")
        }
    }

    private func request<T: Decodable, Body: Encodable>(path: String, body: Body) async throws -> T {
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try encoder.encode(body)
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw AuthenticationError.invalidResponse }
        if response.statusCode == 401 { throw AuthenticationError.unauthorized }
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
        do {
            let activeSession: StoredSession
            if session.expiresAt <= .now {
                activeSession = StoredSession(
                    response: try await AuthAPIClient().refresh(refreshToken: session.refreshToken)
                )
                try KeychainSessionStore.save(activeSession)
                self.session = activeSession
            } else {
                activeSession = session
            }
            try await AuthAPIClient().signOut(accessToken: activeSession.accessToken)
            clearSession()
        } catch AuthenticationError.unauthorized {
            clearSession()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func updateSession(_ operation: () async throws -> AuthResponse) async {
        isWorking = true
        defer { isWorking = false }
        do {
            let updated = StoredSession(response: try await operation())
            try KeychainSessionStore.save(updated)
            session = updated
            errorMessage = nil
        } catch AuthenticationError.unauthorized {
            clearSession()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func clearSession() {
        KeychainSessionStore.clear()
        session = nil
        errorMessage = nil
    }
}

@main
struct TeVartaApp: App {
    @StateObject private var authentication = AuthenticationViewModel()

    var body: some Scene {
        WindowGroup {
            Group {
                if authentication.session != nil {
                    TeVartaRootView()
                } else {
                    NavigationStack {
                        ContentView(authentication: authentication)
                    }
                }
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

private struct TeVartaRootView: View {
    @StateObject private var store = FixtureStore()

    var body: some View {
        TabView {
            NavigationStack { FeedView(store: store) }
                .tabItem { Label("Today", systemImage: "house") }
            NavigationStack { GraphView(store: store) }
                .tabItem { Label("Your world", systemImage: "point.3.connected.trianglepath.dotted") }
            NavigationStack { SettingsView(store: store) }
                .tabItem { Label("You", systemImage: "person") }
        }
        .tint(TeVartaTheme.ink)
        .preferredColorScheme(.light)
    }
}

// MARK: - Fixture state

@MainActor
private final class FixtureStore: ObservableObject {
    enum FeedScenario: String, CaseIterable, Identifiable {
        case success, loading, empty, error
        var id: String { rawValue }
        var title: String { rawValue.capitalized }
    }
    enum FeedState { case idle, loading, loaded([FeedCard]), empty, failed(String) }

    @Published var feedState: FeedState = .idle
    @Published var graph: GraphResponse?
    @Published var profile: Profile?
    @Published var graphError: String?
    @Published var profileError: String?
    private let client = FixtureAPIClient()

    func loadFeed(_ scenario: FeedScenario = .success) async {
        switch scenario {
        case .error:
            feedState = .loading
            do {
                _ = try await client.feed(token: "expired-fixture-token")
                feedState = .failed("The fixture server did not reject an expired session.")
            } catch { feedState = .failed(error.localizedDescription) }
            return
        case .success, .loading, .empty:
            feedState = .loading
        }
        do {
            let response = try await client.feed(scenario: scenario == .success ? nil : scenario.rawValue)
            feedState = response.items.isEmpty ? .empty : .loaded(response.items)
        } catch FixtureAPIError.loading {
            feedState = .loading
        } catch { feedState = .failed(error.localizedDescription) }
    }

    func loadGraph() async {
        guard graph == nil else { return }
        graphError = nil
        do { graph = try await client.graph() }
        catch { graphError = error.localizedDescription }
    }
    func loadProfile() async {
        guard profile == nil else { return }
        profileError = nil
        do { profile = try await client.profile().profile }
        catch { profileError = error.localizedDescription }
    }
    func search(_ query: String) async throws -> EntitySearchResponse { try await client.search(query: query) }
    func event(_ eventID: String) async throws -> EventDetailResponse { try await client.event(eventID: eventID) }
    func entity(_ entityID: String) async throws -> EntityResponse { try await client.entity(entityID: entityID) }
}

// MARK: - Onboarding

private struct OnboardingView: View {
    @ObservedObject var store: FixtureStore
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var result: EntitySearchResponse?
    @State private var isSearching = false
    @State private var error: String?
    @State private var selected: EntityCandidate?
    @State private var showConstruction = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 28) {
                    Text("STEP ONE OF ONE").tevartaLabel()
                    Text("What matters in your world right now?")
                        .font(.system(.largeTitle, design: .serif, weight: .bold))
                        .fixedSize(horizontal: false, vertical: true)
                    Text("Add the people, places, companies, or systems you want Te Vārtā to watch.")
                        .foregroundStyle(TeVartaTheme.muted)
                    TextField("Try Tesla or Apple", text: $query)
                        .textFieldStyle(.plain)
                        .padding(.vertical, 14)
                        .overlay(alignment: .bottom) { Divider().background(TeVartaTheme.rule) }
                        .submitLabel(.search)
                        .onSubmit { Task { await search() } }
                    if isSearching {
                        ProgressView("Searching your world…").tint(TeVartaTheme.oxide)
                    } else if let result {
                        SearchResultList(result: result) { candidate in
                            selected = candidate
                            showConstruction = true
                        }
                    } else {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("START WITH").tevartaLabel()
                            Text("A company you hold, your city, a major decision, or the work you do.")
                                .foregroundStyle(TeVartaTheme.muted)
                        }
                    }
                    if let error { ErrorCallout(message: error) { Task { await search() } } }
                }
                .padding(24)
            }
            .background(TeVartaTheme.paper.ignoresSafeArea())
            .navigationTitle("Te Vārtā")
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } } }
            .navigationDestination(isPresented: $showConstruction) {
                GraphConstructionView(entity: selected, store: store) { dismiss() }
            }
        }
    }

    private func search() async {
        let term = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !term.isEmpty else { return }
        isSearching = true
        error = nil
        defer { isSearching = false }
        do { result = try await store.search(term) }
        catch { self.error = error.localizedDescription }
    }
}

private struct SearchResultList: View {
    let result: EntitySearchResponse
    let select: (EntityCandidate) -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if result.candidates.isEmpty {
                Text("NO MATCHES YET").tevartaLabel()
                Text("Try a company name, ticker, place, or organization.")
                    .foregroundStyle(TeVartaTheme.muted)
            } else if result.ambiguous {
                Text("WE FOUND MORE THAN ONE MATCH").tevartaLabel()
                Text("Choose the one you mean.").font(.title3.weight(.semibold))
            }
            ForEach(result.candidates) { candidate in
                Button { select(candidate) } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(candidate.name).font(.headline)
                            Text(candidate.subtitle).tevartaLabel(color: TeVartaTheme.muted)
                        }
                        Spacer(); Image(systemName: "arrow.right")
                    }.padding(.vertical, 14)
                }
                .buttonStyle(.plain)
                Divider()
            }
            }
        }
    }

private struct GraphConstructionView: View {
    let entity: EntityCandidate?
    @ObservedObject var store: FixtureStore
    let finish: () -> Void
    @State private var isBuilt = false
    var body: some View {
        VStack(spacing: 28) {
            Spacer()
            Image(systemName: isBuilt ? "point.3.connected.trianglepath.dotted" : "circle.dotted")
                .font(.system(size: 72, weight: .thin)).foregroundStyle(TeVartaTheme.oxide)
                .symbolEffect(.pulse, options: .repeating, isActive: !isBuilt)
            Text(isBuilt ? "Your world, mapped." : "Mapping your world")
                .font(.system(.largeTitle, design: .serif, weight: .bold))
            Text(isBuilt ? "We’ll connect the evidence behind the things you care about." : "Building the first connections around \(entity?.name ?? "your selection").")
                .multilineTextAlignment(.center).foregroundStyle(TeVartaTheme.muted)
            Spacer()
            if isBuilt { Button("BEGIN") { finish() }.tevartaPrimaryButton() }
            else if let error = store.graphError { ErrorCallout(message: error) { Task { await construct() } } }
            else { ProgressView("Constructing fixture graph…").tint(TeVartaTheme.oxide) }
        }
        .padding(24).background(TeVartaTheme.paper.ignoresSafeArea())
        .task { await construct() }
    }

    private func construct() async {
        await store.loadGraph()
        guard store.graph != nil else { return }
        try? await Task.sleep(for: .seconds(0.7))
        isBuilt = true
    }
}

// MARK: - Feed and detail

private struct FeedView: View {
    @ObservedObject var store: FixtureStore
    @State private var onboardingPresented = false
    @State private var selectedScenario: FixtureStore.FeedScenario = .success
    var body: some View {
        Group {
            switch store.feedState {
            case .idle, .loading: LoadingView(title: "Preparing your briefing")
            case let .loaded(items): FeedList(items: items, store: store)
            case .empty: EmptyFeedView { onboardingPresented = true }
            case let .failed(message): ErrorCallout(message: message) { Task { await store.loadFeed(selectedScenario) } }.padding(24)
            }
        }
        .background(TeVartaTheme.paper.ignoresSafeArea())
        .navigationTitle("Te Vārtā")
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Menu {
                    Picker("Fixture state", selection: $selectedScenario) {
                        ForEach(FixtureStore.FeedScenario.allCases) { Text($0.title).tag($0) }
                    }
                } label: { Image(systemName: "slider.horizontal.3") }
                .accessibilityLabel("Choose feed fixture state")
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button { onboardingPresented = true } label: { Image(systemName: "plus") }
                    .accessibilityLabel("Add to your world")
            }
        }
        .sheet(isPresented: $onboardingPresented) { OnboardingView(store: store) }
        .task(id: selectedScenario) { await store.loadFeed(selectedScenario) }
    }
}

private struct FeedList: View {
    let items: [FeedCard]
    @ObservedObject var store: FixtureStore
    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("TODAY").tevartaLabel()
                    Text("Your briefing").font(.system(.largeTitle, design: .serif, weight: .bold))
                    Text("\(items.count) SIGNAL\(items.count == 1 ? "" : "S") · EVIDENCE-BACKED").tevartaLabel(color: TeVartaTheme.muted)
                }.padding(.horizontal, 24).padding(.vertical, 24)
                ForEach(items) { card in
                    NavigationLink { EventDetailView(eventID: card.eventId, seed: card, store: store) } label: { FeedCardView(card: card) }
                        .buttonStyle(.plain)
                    Divider().padding(.leading, 24)
                }
            }
        }.refreshable { await store.loadFeed() }
    }
}

private struct FeedCardView: View {
    let card: FeedCard
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack { Text(card.category).tevartaLabel(color: TeVartaTheme.oxide); Spacer(); Text(card.timeLabel).tevartaLabel(color: TeVartaTheme.muted) }
            Text(card.title).font(.system(.title2, design: .serif, weight: .bold)).foregroundStyle(TeVartaTheme.ink)
            Text("WHY — \(card.why)").font(.subheadline).foregroundStyle(TeVartaTheme.muted).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 12) {
                Label("\(card.hops) hops", systemImage: "point.3.connected.trianglepath.dotted")
                Label("\(Int(card.confidence * 100))% confidence", systemImage: "checkmark.seal")
            }.font(.caption).foregroundStyle(TeVartaTheme.muted)
        }.padding(24)
    }
}

private struct EventDetailView: View {
    let eventID: String
    let seed: FeedCard
    @ObservedObject var store: FixtureStore
    @State private var detail: EventDetailResponse?
    @State private var error: String?
    var body: some View {
        ScrollView {
            if let detail {
                VStack(alignment: .leading, spacing: 22) {
                    Text(detail.event.category).tevartaLabel(color: TeVartaTheme.oxide)
                    Text(detail.event.title).font(.system(.largeTitle, design: .serif, weight: .bold))
                    Text("\(detail.event.primarySource.uppercased()) · \(detail.event.articleCount) SOURCES").tevartaLabel(color: TeVartaTheme.muted)
                    EvidenceCallout(card: detail.event)
                    VStack(alignment: .leading, spacing: 12) {
                        Text("SOURCE REPORTING").tevartaLabel()
                        ForEach(detail.articles) { article in
                            Link(destination: article.url) {
                                HStack(alignment: .top) {
                                    VStack(alignment: .leading, spacing: 4) { Text(article.publisher).tevartaLabel(color: TeVartaTheme.oxide); Text(article.title).foregroundStyle(TeVartaTheme.ink) }
                                    Spacer(); Image(systemName: "arrow.up.right")
                                }.padding(.vertical, 10)
                            }; Divider()
                        }
                    }
                    NavigationLink { GraphView(store: store, focus: detail.event.path) } label: {
                        Label("SEE IN YOUR GRAPH", systemImage: "point.3.connected.trianglepath.dotted").tevartaPrimaryButton()
                    }
                }.padding(24)
            } else if let error { ErrorCallout(message: error) { Task { await load() } }.padding(24) }
            else { LoadingView(title: "Loading event evidence") }
        }
        .background(TeVartaTheme.paper.ignoresSafeArea()).navigationTitle("Signal").navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }
    private func load() async { do { detail = try await store.event(eventID) } catch { self.error = error.localizedDescription } }
}

private struct EvidenceCallout: View {
    let card: FeedCard
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("WHY THIS REACHED YOU").tevartaLabel(color: TeVartaTheme.oxide)
            Text(card.why).font(.body.weight(.medium))
            Text("Path: \(card.path.joined(separator: " → "))").font(.footnote).foregroundStyle(TeVartaTheme.muted)
        }.padding(18).overlay { RoundedRectangle(cornerRadius: 2).stroke(TeVartaTheme.rule) }
    }
}

private struct EmptyFeedView: View {
    let addInterest: () -> Void
    var body: some View {
        VStack(spacing: 18) {
            Spacer(); Image(systemName: "checkmark.circle").font(.system(size: 54, weight: .light)).foregroundStyle(TeVartaTheme.oxide)
            Text("Nothing new affects you right now.").font(.system(.title, design: .serif, weight: .bold)).multilineTextAlignment(.center)
            Text("WE’LL KEEP WATCHING.").tevartaLabel(color: TeVartaTheme.muted)
            Button("Add something to watch") { addInterest() }.tevartaSecondaryButton()
            Spacer()
        }.padding(24)
    }
}

// MARK: - Graph and entity detail

private struct GraphView: View {
    @ObservedObject var store: FixtureStore
    var focus: [String] = []
    var body: some View {
        ScrollView {
            if let graph = store.graph {
                VStack(alignment: .leading, spacing: 22) {
                    VStack(alignment: .leading, spacing: 6) { Text("YOUR WORLD").tevartaLabel(); Text("The connections behind what matters.").font(.system(.title, design: .serif, weight: .bold)) }
                    GraphDiagram(graph: graph, focus: focus)
                    VStack(alignment: .leading, spacing: 0) {
                        Text("ENTITIES").tevartaLabel().padding(.bottom, 8)
                        ForEach(graph.nodes) { node in
                            if node.id == "ent_tsla" {
                                NavigationLink { EntityDetailView(entityID: node.id, displayName: node.name, store: store) } label: {
                                    entityRow(node, navigable: true)
                                }.buttonStyle(.plain)
                            } else {
                                entityRow(node, navigable: false)
                            }
                            Divider()
                        }
                    }
                }.padding(24)
            } else if let error = store.graphError { ErrorCallout(message: error) { Task { await store.loadGraph() } }.padding(24) }
            else { LoadingView(title: "Mapping your world") }
        }
        .background(TeVartaTheme.paper.ignoresSafeArea()).navigationTitle("Your world").task { await store.loadGraph() }
    }

    private func entityRow(_ node: GraphNode, navigable: Bool) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 3) {
                Text(node.name).font(.headline)
                Text(node.entityType.uppercased()).tevartaLabel(color: TeVartaTheme.muted)
            }
            Spacer()
            if navigable { Image(systemName: "chevron.right").foregroundStyle(TeVartaTheme.muted) }
        }
        .padding(.vertical, 14)
    }
}

private struct GraphDiagram: View {
    let graph: GraphResponse
    let focus: [String]
    var body: some View {
        VStack(spacing: 0) {
            if graph.edges.isEmpty {
                ForEach(graph.nodes) { node in nodeRow(node) }
            } else {
                ForEach(graph.edges) { edge in
                    if let source = graph.nodes.first(where: { $0.id == edge.sourceId }),
                       let target = graph.nodes.first(where: { $0.id == edge.targetId }) {
                        nodeRow(source)
                        HStack(spacing: 8) { Rectangle().fill(TeVartaTheme.rule).frame(width: 1, height: 20).padding(.leading, 5); Text(edge.relation.replacingOccurrences(of: "_", with: " ")).tevartaLabel(color: TeVartaTheme.muted); Spacer() }
                        nodeRow(target)
                    }
                }
            }
        }.padding(18).overlay { RoundedRectangle(cornerRadius: 2).stroke(TeVartaTheme.rule) }
    }

    private func nodeRow(_ node: GraphNode) -> some View {
        HStack { Circle().fill(focus.contains(node.name) ? TeVartaTheme.oxide : TeVartaTheme.ink).frame(width: 12, height: 12); Text(node.name).font(.body.weight(.semibold)); Spacer(); if focus.contains(node.name) { Text("ON PATH").tevartaLabel(color: TeVartaTheme.oxide) } }.padding(.vertical, 14)
    }
}

private struct EntityDetailView: View {
    let entityID: String
    let displayName: String
    @ObservedObject var store: FixtureStore
    @State private var entity: EntityResponse?
    @State private var error: String?
    var body: some View {
        ScrollView {
            if let entity {
                VStack(alignment: .leading, spacing: 24) {
                    Text(entity.graphStatus.uppercased() + " ENTITY").tevartaLabel(color: TeVartaTheme.oxide)
                    Text(entity.entity.name).font(.system(.largeTitle, design: .serif, weight: .bold))
                    if let ticker = entity.entity.ticker { Text(ticker).tevartaLabel(color: TeVartaTheme.muted) }
                    VStack(alignment: .leading, spacing: 10) { Text("IN YOUR WORLD").tevartaLabel(); Text("This entity is available in the shared graph. Its connections determine when a signal is relevant to you.").foregroundStyle(TeVartaTheme.muted) }
                    Button("REMOVE FROM MY WORLD", role: .destructive) {}.tevartaSecondaryButton(color: TeVartaTheme.oxide).accessibilityHint("Removal is a fixture-only shell in T13")
                }.padding(24)
            } else if let error { ErrorCallout(message: error) { Task { await load() } }.padding(24) }
            else { LoadingView(title: "Loading \(displayName)") }
        }
        .background(TeVartaTheme.paper.ignoresSafeArea()).navigationTitle("Entity").navigationBarTitleDisplayMode(.inline).task { await load() }
    }
    private func load() async { do { entity = try await store.entity(entityID) } catch { self.error = error.localizedDescription } }
}

// MARK: - Settings and shared components

private struct SettingsView: View {
    @ObservedObject var store: FixtureStore
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                Text("You").font(.system(.largeTitle, design: .serif, weight: .bold))
                if let profile = store.profile {
                    SettingsSection(title: "BRIEFING") { SettingsRow(label: "Delivery", value: "Daily morning"); SettingsRow(label: "Notification threshold", value: "Top \(100 - profile.notificationPercentile)%") }
                    SettingsSection(title: "QUIET HOURS") { SettingsRow(label: "Timezone", value: profile.timezone); SettingsRow(label: "Quiet time", value: "\(profile.quietStart)–\(profile.quietEnd)") }
                    SettingsSection(title: "YOUR WORLD") { SettingsRow(label: "Saved signals", value: "View"); SettingsRow(label: "Privacy", value: "Manage") }
                    Text("Settings are read-only fixture shells in T13. Persisted edits land in T42.").font(.footnote).foregroundStyle(TeVartaTheme.muted)
                } else if let error = store.profileError { ErrorCallout(message: error) { Task { await store.loadProfile() } } }
                else { LoadingView(title: "Loading profile") }
            }.padding(24)
        }
        .background(TeVartaTheme.paper.ignoresSafeArea()).navigationTitle("Te Vārtā").task { await store.loadProfile() }
    }
}

private struct SettingsSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content
    var body: some View { VStack(alignment: .leading, spacing: 0) { Text(title).tevartaLabel().padding(.bottom, 6); content } }
}

private struct SettingsRow: View {
    let label: String
    let value: String
    var body: some View {
        HStack { Text(label); Spacer(); Text(value).foregroundStyle(TeVartaTheme.muted); Image(systemName: "chevron.right").font(.caption).foregroundStyle(TeVartaTheme.muted) }
            .padding(.vertical, 14).overlay(alignment: .bottom) { Divider().background(TeVartaTheme.rule) }
    }
}

private struct LoadingView: View {
    let title: String
    var body: some View {
        VStack(spacing: 16) { ProgressView().controlSize(.large).tint(TeVartaTheme.oxide); Text(title).font(.system(.title3, design: .serif, weight: .semibold)); Text("This is a fixture loading state.").font(.footnote).foregroundStyle(TeVartaTheme.muted) }
            .frame(maxWidth: .infinity, maxHeight: .infinity).padding(24)
    }
}

private struct ErrorCallout: View {
    let message: String
    let retry: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 12) { Text("WE COULDN’T LOAD THIS").tevartaLabel(color: TeVartaTheme.oxide); Text(message).foregroundStyle(TeVartaTheme.muted); Button("Try again") { retry() }.tevartaSecondaryButton() }
            .padding(18).overlay { RoundedRectangle(cornerRadius: 2).stroke(TeVartaTheme.oxide.opacity(0.5)) }
    }
}

private enum TeVartaTheme {
    static let paper = Color(red: 0.98, green: 0.973, blue: 0.949)
    static let ink = Color(red: 0.102, green: 0.094, blue: 0.082)
    static let oxide = Color(red: 0.545, green: 0.18, blue: 0.133)
    static let muted = Color(red: 0.42, green: 0.4, blue: 0.38)
    static let rule = Color(red: 0.81, green: 0.79, blue: 0.74)
}

private extension Text {
    func tevartaLabel(color: Color = TeVartaTheme.ink) -> some View {
        font(.system(.caption2, design: .monospaced, weight: .bold)).tracking(1.1).foregroundStyle(color)
    }
}
private extension View {
    func tevartaPrimaryButton() -> some View {
        font(.system(.footnote, design: .monospaced, weight: .bold)).tracking(1).foregroundStyle(TeVartaTheme.paper).frame(maxWidth: .infinity).padding(.vertical, 15).background(TeVartaTheme.ink)
    }
    func tevartaSecondaryButton(color: Color = TeVartaTheme.ink) -> some View {
        font(.system(.footnote, design: .monospaced, weight: .bold)).tracking(0.7).foregroundStyle(color).padding(.vertical, 8)
    }
}

// MARK: - Fixture HTTP client and frozen response models

private enum FixtureAPIError: LocalizedError {
    case configuration, invalidResponse, loading, server(String)
    var errorDescription: String? {
        switch self {
        case .configuration: return "The fixture API endpoint has not been configured."
        case .invalidResponse: return "The fixture server returned an invalid response."
        case .loading: return "The fixture is still loading."
        case let .server(message): return message
        }
    }
}

private struct FixtureAPIClient {
    private let baseURL: URL
    private let session: URLSession
    private let decoder: JSONDecoder
    init(bundle: Bundle = .main, session: URLSession = .shared) {
        let configured = bundle.object(forInfoDictionaryKey: "TEVARTA_API_BASE_URL") as? String
        baseURL = URL(string: configured ?? "http://127.0.0.1:8000")!
        self.session = session; decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
    }
    func feed(scenario: String? = nil, token: String = "fixture-access-token") async throws -> FeedResponse { try await get("v1/feed", query: scenario.map { [URLQueryItem(name: "scenario", value: $0)] }, token: token) }
    func search(query: String) async throws -> EntitySearchResponse { try await get("v1/entities/search", query: [URLQueryItem(name: "q", value: query)]) }
    func event(eventID: String) async throws -> EventDetailResponse { try await get("v1/events/\(eventID)") }
    func graph() async throws -> GraphResponse { try await get("v1/graph") }
    func entity(entityID: String) async throws -> EntityResponse { try await get("v1/entities/\(entityID)") }
    func profile() async throws -> ProfileResponse { try await get("v1/profile") }
    private func get<T: Decodable>(_ path: String, query: [URLQueryItem]? = nil, token: String = "fixture-access-token") async throws -> T {
        var components = URLComponents(url: baseURL.appending(path: path), resolvingAgainstBaseURL: false); components?.queryItems = query
        guard let url = components?.url else { throw FixtureAPIError.configuration }
        var request = URLRequest(url: url); request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw FixtureAPIError.invalidResponse }
        if http.statusCode == 202 {
            _ = try? decoder.decode(LoadingResponse.self, from: data)
            throw FixtureAPIError.loading
        }
        guard (200 ..< 300).contains(http.statusCode) else {
            let apiError = try? decoder.decode(ErrorEnvelope.self, from: data)
            throw FixtureAPIError.server(apiError?.error.message ?? "The fixture request failed (\(http.statusCode)).")
        }
        do { return try decoder.decode(T.self, from: data) } catch { throw FixtureAPIError.invalidResponse }
    }
}

private struct EntityCandidate: Decodable, Identifiable {
    let id: String; let name: String; let entityType: String; let ticker: String?; let lifecycle: String
    var subtitle: String { [ticker, entityType, lifecycle].compactMap { $0 }.joined(separator: " · ").uppercased() }
}
private struct EntitySearchResponse: Decodable { let query: String; let candidates: [EntityCandidate]; let ambiguous: Bool }
private struct FeedResponse: Decodable { let items: [FeedCard]; let nextCursor: String? }
private struct FeedCard: Decodable, Identifiable {
    let eventId: String; let revision: Int; let category: String; let title: String; let why: String; let score: Int; let confidence: Double; let hops: Int; let importance: Double; let publishedAt: String; let articleCount: Int; let primarySource: String; let entities: [EntityCandidate]; let path: [String]; let slotType: String; let slotPosition: Int
    var id: String { eventId }
    var timeLabel: String { publishedAt.split(separator: "T").last?.prefix(5).description ?? "" }
}
private struct EventArticle: Decodable, Identifiable { let id: String; let publisher: String; let title: String; let url: URL; let publishedAt: String }
private struct EventDetailResponse: Decodable { let requestedEventId: String; let event: FeedCard; let articles: [EventArticle]; let mergedFrom: [String] }
private struct GraphNode: Decodable, Identifiable { let id: String; let name: String; let entityType: String }
private struct GraphEdge: Decodable, Identifiable { let sourceId: String; let targetId: String; let relation: String; let confidence: Double; var id: String { "\(sourceId)-\(relation)-\(targetId)" } }
private struct GraphResponse: Decodable { let nodes: [GraphNode]; let edges: [GraphEdge] }
private struct EntityResponse: Decodable { let entity: EntityCandidate; let graphStatus: String }
private struct Profile: Decodable { let timezone: String; let notificationPercentile: Int; let quietStart: String; let quietEnd: String }
private struct ProfileResponse: Decodable { let profile: Profile }
private struct LoadingResponse: Decodable { let state: String; let retryAfterSeconds: Int }
private struct ErrorEnvelope: Decodable { let error: APIError }
private struct APIError: Decodable { let code: String; let message: String }
